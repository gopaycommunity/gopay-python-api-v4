from __future__ import annotations

import contextvars
import logging
import pickle
import threading
import time
import uuid
from typing import Any, Callable

import httpx
import pytest

from gopay_payments import ErrorCode, GoPayClient, GoPayError, GoPayHTTPError, GoPaySDKError

from .conftest import TOKEN_PATH, ErrorLog, FakeGateway, reply

TRACE = "x-gopay-trace-id"
TRANSACTION = "x-gopay-transaction-id"
PAYMENT_SESSION = "x-gopay-payment-session-id"


def is_uuid4(value: str) -> bool:
    try:
        return uuid.UUID(value).version == 4 and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def ids_of(request: httpx.Request) -> tuple[str, str]:
    return request.headers[TRACE], request.headers[TRANSACTION]


# ----------------------------------------------------------------- the headers


def test_every_request_carries_a_trace_and_a_transaction_id(
    make_client: Callable[..., GoPayClient], gateway: FakeGateway
) -> None:
    client = make_client()
    client.authenticate("client-id", "client-secret", "payment:read")
    gateway.on("GET", "/payments/1", reply(200, {"id": "1"}))
    client.get_payment_status("1")
    token_request, api_request = gateway.requests
    for request in (token_request, api_request):
        assert is_uuid4(request.headers[TRACE])
        assert is_uuid4(request.headers[TRANSACTION])


def test_the_payment_session_id_is_the_payment_in_the_path(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", "/payments/300000001/charge", reply(200, {"id": "c", "state": "PENDING"}))
    gateway.on("POST", "/eshops/8123456789/links", reply(201, {"id": "l", "url": "u"}))
    client.get_charge_state("300000001")
    assert gateway.last().headers[PAYMENT_SESSION] == "300000001"
    client.create_payment_link(8123456789, {"amount": 100})  # type: ignore[typeddict-item]
    assert PAYMENT_SESSION not in gateway.last().headers
    assert all(PAYMENT_SESSION not in r.headers for r in gateway.token_requests())


def test_the_payment_session_id_stays_percent_encoded(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", "/payments/a%20b", reply(200, {"id": "a b"}))
    client.get_payment_status("a b")
    assert gateway.last().headers[PAYMENT_SESSION] == "a%20b"


def test_one_call_shares_its_transaction_id_and_gives_each_request_a_trace_id(
    make_client: Callable[..., GoPayClient], gateway: FakeGateway
) -> None:
    client = make_client()
    client.authenticate("client-id", "client-secret", "payment:read")
    client._transport._clock = lambda: time.time() + 880  # the call has to refresh the token
    gateway.on("GET", "/payments/1", reply(200, {"id": "1"}))
    client.get_payment_status("1")
    refresh, api_request = gateway.requests[1:]
    assert refresh.url.path.endswith(TOKEN_PATH)
    assert refresh.headers[TRANSACTION] == api_request.headers[TRANSACTION]
    assert refresh.headers[TRACE] != api_request.headers[TRACE]


def test_each_call_gets_a_transaction_id_of_its_own(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", "/payments/1", reply(200, {"id": "1"}))
    client.get_payment_status("1")
    client.get_payment_status("1")
    first, second = gateway.api_requests()
    assert first.headers[TRANSACTION] != second.headers[TRANSACTION]
    assert first.headers[TRACE] != second.headers[TRACE]


def test_a_5xx_retry_keeps_the_trace_id(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/payments/1", reply(503), reply(502), reply(200, {"id": "1"}))
    client.get_payment_status("1")
    attempts = gateway.api_requests()
    assert len(attempts) == 3
    assert len({ids_of(r) for r in attempts}) == 1


def test_the_resend_after_a_401_keeps_the_trace_id(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", "/payments/1", reply(401), reply(200, {"id": "1"}))
    client.get_payment_status("1")
    first, second = gateway.api_requests()
    assert ids_of(first) == ids_of(second)
    assert first.headers["authorization"] != second.headers["authorization"]
    refresh = gateway.token_requests()[-1]
    assert refresh.headers[TRANSACTION] == first.headers[TRANSACTION]


def test_the_polls_of_an_await_method_share_one_transaction(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on(
        "GET",
        "/payments/42/charge",
        reply(200, {"id": "c", "state": "PENDING"}),
        reply(200, {"id": "c", "state": "PROCESSING"}),
        reply(200, {"id": "c", "state": "SUCCEEDED"}),
    )
    client.await_charge_state("42", interval=1)
    polls = gateway.api_requests()
    assert len(polls) == 3
    assert len({r.headers[TRANSACTION] for r in polls}) == 1
    assert len({r.headers[TRACE] for r in polls}) == 3


def test_a_call_from_a_callback_joins_the_running_transaction(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on(
        "GET",
        "/payments/42",
        reply(200, {"id": "42", "state": "CREATED"}),
        reply(200, {"id": "42", "state": "PAID"}),
    )
    gateway.on("GET", "/refunds/r-1", reply(200, {"id": "r-1", "state": "SUCCESS"}))
    client.await_payment_status(
        "42", interval=1, on_state_change=lambda _payment: client.get_refund("r-1")
    )
    assert len({r.headers[TRANSACTION] for r in gateway.api_requests()}) == 1


# ----------------------------------------------------------- with_transaction_id


def test_calls_inside_the_block_use_the_given_transaction_id(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", "/payments/1", reply(200, {"id": "1"}))
    with client.with_transaction_id("order-2026.11_24"):
        client.get_payment_status("1")
        client.get_payment_status("1")
    client.get_payment_status("1")
    inside_1, inside_2, after = gateway.api_requests()
    assert inside_1.headers[TRANSACTION] == inside_2.headers[TRANSACTION] == "order-2026.11_24"
    assert inside_1.headers[TRACE] != inside_2.headers[TRACE]
    assert is_uuid4(after.headers[TRANSACTION])


def test_an_inner_block_wins_and_the_outer_one_comes_back(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", "/payments/1", reply(200, {"id": "1"}))
    with client.with_transaction_id("outer"):
        with client.with_transaction_id("inner"):
            client.get_payment_status("1")
        client.get_payment_status("1")
    assert [r.headers[TRANSACTION] for r in gateway.api_requests()] == ["inner", "outer"]


def test_the_id_is_restored_when_the_block_raises(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", "/payments/1", reply(200, {"id": "1"}))
    with pytest.raises(RuntimeError), client.with_transaction_id("doomed"):
        raise RuntimeError
    client.get_payment_status("1")
    assert gateway.last().headers[TRANSACTION] != "doomed"


def test_the_token_request_of_a_call_in_the_block_uses_the_id_too(
    make_client: Callable[..., GoPayClient], gateway: FakeGateway
) -> None:
    client = make_client()
    with client.with_transaction_id("checkout-7"):
        client.authenticate("client-id", "client-secret", "payment:read")
    assert gateway.token_requests()[0].headers[TRANSACTION] == "checkout-7"


@pytest.mark.parametrize(
    "value",
    ["", "with space", "x" * 65, "objednávka", "a/b", "line\nbreak", None, 42],
)
def test_an_invalid_transaction_id_is_refused_and_reported(
    client: GoPayClient, gateway: FakeGateway, errors: ErrorLog, value: Any
) -> None:
    with pytest.raises(GoPaySDKError) as caught:
        client.with_transaction_id(value)
    assert caught.value.error_code is ErrorCode.INVALID_ARGUMENT
    assert caught.value.trace_id is None
    assert errors.errors == [caught.value]
    assert gateway.api_requests() == []


def test_sixty_four_characters_are_accepted(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/payments/1", reply(200, {"id": "1"}))
    with client.with_transaction_id("A" * 64):
        client.get_payment_status("1")
    assert gateway.last().headers[TRANSACTION] == "A" * 64


def test_threads_keep_their_own_transaction_ids(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/payments/1", reply(200, {"id": "1"}))
    barrier = threading.Barrier(2)
    seen: dict[str, list[str]] = {}

    def worker(name: str) -> None:
        with client.with_transaction_id(name):
            barrier.wait()  # both blocks are open at once
            client.get_payment_status("1")
            barrier.wait()
            client.get_payment_status("1")

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("thread-a", "thread-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for request in gateway.api_requests():
        seen.setdefault(request.headers[TRANSACTION], []).append(request.headers[TRACE])
    assert sorted(seen) == ["thread-a", "thread-b"]
    assert all(len(traces) == 2 for traces in seen.values())


def test_a_thread_started_in_the_block_inherits_only_a_copied_context(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", "/payments/1", reply(200, {"id": "1"}))
    with client.with_transaction_id("parent"):
        plain = threading.Thread(target=client.get_payment_status, args=("1",))
        plain.start()
        plain.join()
        copied = threading.Thread(
            target=contextvars.copy_context().run, args=(client.get_payment_status, "1")
        )
        copied.start()
        copied.join()
    plain_request, copied_request = gateway.api_requests()
    assert plain_request.headers[TRANSACTION] != "parent"
    assert copied_request.headers[TRANSACTION] == "parent"


# ---------------------------------------------------------------- ids on errors


def test_an_http_error_carries_the_ids_of_its_request(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", "/payments/1", reply(404, {"errors": [{"error_code": 404}]}))
    with pytest.raises(GoPayHTTPError) as caught:
        client.get_payment_status("1")
    assert (caught.value.trace_id, caught.value.transaction_id) == ids_of(gateway.last())


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        pytest.param(httpx.ReadTimeout("slow"), ErrorCode.NETWORK_TIMEOUT, id="timeout"),
        pytest.param(httpx.ConnectError("down"), ErrorCode.NETWORK_ERROR, id="network"),
    ],
)
def test_an_error_without_a_response_still_carries_the_ids(
    client: GoPayClient, gateway: FakeGateway, failure: Exception, code: ErrorCode
) -> None:
    gateway.on("GET", "/payments/1", failure)
    with pytest.raises(GoPaySDKError) as caught:
        client.get_payment_status("1")
    assert caught.value.error_code is code
    assert (caught.value.trace_id, caught.value.transaction_id) == ids_of(gateway.last())


def test_an_unusable_body_carries_the_ids(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/payments/1", reply(200, text="<html>"))
    with pytest.raises(GoPaySDKError) as caught:
        client.get_payment_status("1")
    assert caught.value.error_code is ErrorCode.UNEXPECTED_RESPONSE
    assert caught.value.trace_id == gateway.last().headers[TRACE]


def test_a_second_401_carries_the_ids(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/payments/1", reply(401))
    with pytest.raises(GoPaySDKError) as caught:
        client.get_payment_status("1")
    assert caught.value.error_code is ErrorCode.AUTH_UNAUTHORIZED
    assert caught.value.trace_id == gateway.last().headers[TRACE]


def test_a_failed_refresh_carries_the_ids_of_the_token_request(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("POST", TOKEN_PATH, reply(503))
    client._transport._clock = lambda: time.time() + 880
    with pytest.raises(GoPaySDKError) as caught:
        client.get_payment_status("1")
    assert caught.value.error_code is ErrorCode.AUTH_REFRESH_FAILED
    refresh = gateway.token_requests()[-1]
    assert (caught.value.trace_id, caught.value.transaction_id) == ids_of(refresh)
    assert gateway.api_requests() == []


def test_a_failed_authenticate_carries_the_ids_of_the_token_request(
    make_client: Callable[..., GoPayClient], gateway: FakeGateway
) -> None:
    gateway.on("POST", TOKEN_PATH, reply(401, {"error": "invalid_client"}))
    client = make_client()
    with pytest.raises(GoPayHTTPError) as caught:
        client.authenticate("client-id", "wrong", "payment:read")
    assert caught.value.trace_id == gateway.token_requests()[0].headers[TRACE]


def test_an_error_before_any_request_carries_no_ids(
    make_client: Callable[..., GoPayClient], gateway: FakeGateway
) -> None:
    client = make_client()
    with pytest.raises(GoPaySDKError) as missing_token:
        client.get_payment_status("1")
    assert missing_token.value.error_code is ErrorCode.AUTH_TOKEN_MISSING
    with pytest.raises(GoPaySDKError) as invalid:
        client.get_payment_status("")
    for error in (missing_token.value, invalid.value):
        assert error.trace_id is None
        assert error.transaction_id is None
    assert gateway.requests == []


def test_the_ids_survive_pickle(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/payments/1", reply(409, {"errors": []}))
    with pytest.raises(GoPayError) as caught:
        client.get_payment_status("1")
    clone = pickle.loads(pickle.dumps(caught.value))  # noqa: S301 — our own object
    assert (clone.trace_id, clone.transaction_id) == ids_of(gateway.last())


# ------------------------------------------------------------------ debug log


def test_the_debug_log_names_the_trace_id_of_every_attempt(
    client: GoPayClient, gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    gateway.on("GET", "/payments/1", httpx.ConnectError("down"), httpx.ReadTimeout("slow"))
    with caplog.at_level(logging.DEBUG, logger="gopay_payments"), pytest.raises(GoPaySDKError):
        client.get_payment_status("1")
    trace = gateway.last().headers[TRACE]
    lines = [r.getMessage() for r in caplog.records if r.name == "gopay_payments"]
    assert any(line.startswith("→ GET") and trace in line for line in lines)
    assert any("ConnectError" in line and trace in line for line in lines)
    assert any("timed out" in line and trace in line for line in lines)
