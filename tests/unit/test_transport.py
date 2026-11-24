from __future__ import annotations

import datetime
import email.utils
import pickle
from typing import Any, Callable, Optional, cast

import httpx
import pytest

from gopay_payments import (
    ErrorCode,
    GoPayClient,
    GoPayError,
    GoPayHTTPError,
    GoPaySDKError,
    __version__,
)

from .conftest import ErrorLog, FakeGateway, reply


def test_every_api_request_sends_the_user_agent(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/payments/1", reply(200, {"id": 1}))
    client.get_payment_status("1")
    for request in gateway.requests:  # the token request included
        assert request.headers["user-agent"] == f"GoPay Python v4 {__version__}"
        assert request.headers["accept"] == "application/json"


def test_suffix_reaches_the_wire_after_the_sdk_user_agent(
    make_client: Callable[..., GoPayClient], gateway: FakeGateway
) -> None:
    client = make_client(user_agent_suffix="MyShop/2.1")
    client.authenticate("id", "secret", "payment:read")
    assert gateway.requests[0].headers["user-agent"] == f"GoPay Python v4 {__version__} MyShop/2.1"


def test_post_without_a_body_still_declares_json(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("POST", "/recurrences/5/start", reply(200, {"id": 9}))
    client.start_recurrence("5")
    request = gateway.last()
    assert request.headers["content-type"] == "application/json"
    assert request.content == b""


@pytest.mark.parametrize("method", ["start_recurrence", "create_next_payment"])
def test_an_empty_override_is_sent_as_an_empty_object(
    client: GoPayClient, gateway: FakeGateway, method: str
) -> None:
    # {} and no override are different requests: the API reads a body that is present.
    action = "start" if method == "start_recurrence" else "next"
    gateway.on("POST", f"/recurrences/5/{action}", reply(201, {"id": 9}))
    getattr(client, method)("5", {})
    assert gateway.last().content == b"{}"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(datetime.date(2027, 1, 31), id="date"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param({1, 2}, id="set"),
    ],
)
def test_a_body_that_is_not_json_is_an_invalid_argument(
    client: GoPayClient, gateway: FakeGateway, errors: ErrorLog, value: object
) -> None:
    params = cast(Any, {"amount": 100, "recurrence_date_to": value})
    with pytest.raises(GoPaySDKError) as caught:
        client.create_recurrence("8123456789", params)
    assert caught.value.error_code is ErrorCode.INVALID_ARGUMENT
    assert isinstance(caught.value.__cause__, (TypeError, ValueError))
    assert errors.errors == [caught.value]
    assert gateway.api_requests() == []


def test_a_circular_body_is_an_invalid_argument(client: GoPayClient, gateway: FakeGateway) -> None:
    params: dict[str, Any] = {"amount": 100}
    params["self"] = params
    body = cast(Any, params)
    with pytest.raises(GoPaySDKError) as caught:
        client.refund_payment("1", body)
    assert caught.value.error_code is ErrorCode.INVALID_ARGUMENT
    assert gateway.api_requests() == []


def test_post_sends_json(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("POST", "/payments/1/refunds", reply(200, {"id": "r1", "state": "REQUESTED"}))
    client.refund_payment("1", {"amount": 100})
    request = gateway.last()
    assert request.headers["content-type"] == "application/json"
    assert request.content == b'{"amount": 100}'


def test_get_sends_no_content_type(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/payments/1", reply(200, {"id": 1}))
    client.get_payment_status("1")
    assert "content-type" not in gateway.last().headers


def test_get_is_retried_on_5xx_then_raises_with_the_last_body(
    client: GoPayClient, gateway: FakeGateway, errors: ErrorLog
) -> None:
    gateway.on("GET", "/payments/1", reply(502), reply(503), reply(503, {"errors": ["busy"]}))
    with pytest.raises(GoPayHTTPError) as caught:
        client.get_payment_status("1")
    assert caught.value.status == 503
    assert caught.value.body == {"errors": ["busy"]}
    assert len(gateway.api_requests()) == 3
    assert errors.errors == [caught.value]


def test_get_recovers_after_a_dropped_connection(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/payments/1", httpx.ConnectError("reset"), reply(200, {"id": 1}))
    assert client.get_payment_status("1")["id"] == 1  # type: ignore[comparison-overlap]
    assert len(gateway.api_requests()) == 2


@pytest.mark.parametrize(("draw", "expected"), [(0.0, [0.1, 0.2]), (1.0, [0.2, 0.4])])
def test_retries_back_off_exponentially_with_jitter(
    client: GoPayClient, gateway: FakeGateway, draw: float, expected: list[float]
) -> None:
    # Half the exponential delay, plus a random part of the other half: clients that failed
    # together do not all come back at the same moment.
    pauses: list[float] = []
    client._transport._sleep = pauses.append
    client._transport._random = lambda: draw
    gateway.on("GET", "/payments/1", reply(500), reply(500), reply(200, {"id": 1}))
    client.get_payment_status("1")
    assert pauses == pytest.approx(expected)


def too_many(retry_after: Optional[str] = None) -> httpx.Response:
    headers = {} if retry_after is None else {"retry-after": retry_after}
    return httpx.Response(429, headers=headers, json={"errors": [{"error_code": 429}]})


def test_a_429_on_a_read_is_retried_after_the_seconds_it_asks_for(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    pauses: list[float] = []
    client._transport._sleep = pauses.append
    gateway.on("GET", "/payments/1", too_many("2"), reply(200, {"id": "1"}))
    assert client.get_payment_status("1")["id"] == "1"
    assert pauses == [2.0]
    assert len(gateway.api_requests()) == 2


def test_a_429_may_name_the_moment_to_retry_at(client: GoPayClient, gateway: FakeGateway) -> None:
    pauses: list[float] = []
    client._transport._sleep = pauses.append
    now = 1_800_000_000.0
    client._transport._clock = lambda: now
    at = email.utils.formatdate(now + 3, usegmt=True)
    gateway.on("GET", "/payments/1", too_many(at), reply(200, {"id": "1"}))
    client.get_payment_status("1")
    assert pauses == [3.0]


@pytest.mark.parametrize("retry_after", [None, "soon", "-1", "1.5"])
def test_a_429_without_a_usable_retry_after_backs_off(
    client: GoPayClient, gateway: FakeGateway, retry_after: Optional[str]
) -> None:
    pauses: list[float] = []
    client._transport._sleep = pauses.append
    client._transport._random = lambda: 1.0
    gateway.on("GET", "/payments/1", too_many(retry_after), reply(200, {"id": "1"}))
    client.get_payment_status("1")
    assert pauses == [0.2]


def test_a_429_asking_for_a_long_wait_goes_to_the_caller(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    pauses: list[float] = []
    client._transport._sleep = pauses.append
    gateway.on("GET", "/payments/1", too_many("60"))
    with pytest.raises(GoPayHTTPError) as caught:
        client.get_payment_status("1")
    assert caught.value.status == 429
    assert caught.value.retry_after == 60.0  # the caller learns how long to wait
    assert pauses == []
    assert len(gateway.api_requests()) == 1


def test_a_write_answered_429_carries_the_wait_it_asked_for(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("POST", "/payments/1/refunds", too_many("5"))
    with pytest.raises(GoPayHTTPError) as caught:
        client.refund_payment("1", {"amount": 1})
    assert caught.value.retry_after == 5.0
    assert len(gateway.api_requests()) == 1


def test_a_retry_after_date_becomes_seconds_on_the_error(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    now = 1_800_000_000.0
    client._transport._clock = lambda: now
    gateway.on("GET", "/payments/1", too_many(email.utils.formatdate(now + 90, usegmt=True)))
    with pytest.raises(GoPayHTTPError) as caught:
        client.get_payment_status("1")
    assert caught.value.retry_after == 90.0


@pytest.mark.parametrize("response", [too_many(None), too_many("soon"), reply(404, {})])
def test_an_error_without_a_usable_retry_after_has_none(
    client: GoPayClient, gateway: FakeGateway, response: httpx.Response
) -> None:
    gateway.on("POST", "/payments/1/refunds", response)
    with pytest.raises(GoPayHTTPError) as caught:
        client.refund_payment("1", {"amount": 1})
    assert caught.value.retry_after is None


def test_retry_after_survives_pickle(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("POST", "/payments/1/refunds", too_many("7"))
    with pytest.raises(GoPayHTTPError) as caught:
        client.refund_payment("1", {"amount": 1})
    clone = pickle.loads(pickle.dumps(caught.value))  # noqa: S301 — our own object
    assert clone.retry_after == 7.0


def test_a_429_that_persists_raises_after_the_last_attempt(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", "/payments/1", too_many("1"))
    with pytest.raises(GoPayHTTPError) as caught:
        client.get_payment_status("1")
    assert caught.value.status == 429
    assert len(gateway.api_requests()) == 3


def test_a_5xx_after_a_429_backs_off_rather_than_waiting_as_asked_before(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    pauses: list[float] = []
    client._transport._sleep = pauses.append
    client._transport._random = lambda: 1.0
    gateway.on("GET", "/payments/1", too_many("5"), reply(503), reply(200, {"id": "1"}))
    client.get_payment_status("1")
    assert pauses == [5.0, 0.4]


def test_a_dropped_connection_after_a_429_backs_off_rather_than_waiting_again(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    pauses: list[float] = []
    client._transport._sleep = pauses.append
    client._transport._random = lambda: 1.0
    gateway.on(
        "GET", "/payments/1", too_many("8"), httpx.ConnectError("reset"), reply(200, {"id": "1"})
    )
    client.get_payment_status("1")
    assert pauses == [8.0, 0.4]


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_a_429_on_a_write_is_not_retried(
    client: GoPayClient, gateway: FakeGateway, method: str
) -> None:
    gateway.on(method, "/payments/1/x", too_many("1"))
    with pytest.raises(GoPayHTTPError):
        client._transport.request(method, "/payments/1/x", body={} if method == "POST" else None)
    assert len(gateway.api_requests()) == 1


def test_post_is_never_retried(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("POST", "/payments/1/charge", reply(503))
    with pytest.raises(GoPayHTTPError):
        client._transport.request("POST", "/payments/1/charge", body={})
    assert len(gateway.api_requests()) == 1


def test_post_network_error(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("POST", "/payments/1/refunds", httpx.ConnectError("reset"))
    with pytest.raises(GoPaySDKError) as caught:
        client.refund_payment("1", {"amount": 1})
    assert caught.value.error_code is ErrorCode.NETWORK_ERROR
    assert str(caught.value) == "[GoPaySDK] Network error (ConnectError)."
    assert len(gateway.api_requests()) == 1


def test_timeout_is_not_retried(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/payments/1", httpx.ReadTimeout("slow"))
    with pytest.raises(GoPaySDKError) as caught:
        client.get_payment_status("1")
    assert caught.value.error_code is ErrorCode.NETWORK_TIMEOUT
    assert len(gateway.api_requests()) == 1


def test_timeout_is_passed_to_every_request(
    make_client: Callable[..., GoPayClient], gateway: FakeGateway
) -> None:
    seen: list[object] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return gateway.handle(request)

    client = make_client(
        timeout=2.5, http_client=httpx.Client(transport=httpx.MockTransport(record))
    )
    client.authenticate("id", "secret", "payment:read")
    assert seen == [{"connect": 2.5, "read": 2.5, "write": 2.5, "pool": 2.5}]


def test_http_error_carries_status_body_and_normalized_endpoint(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", "/payments/3047213485", reply(404, {"errors": [{"error_code": 404}]}))
    with pytest.raises(GoPayHTTPError) as caught:
        client.get_payment_status("3047213485")
    error = caught.value
    assert (error.status, error.method, error.endpoint) == (404, "GET", "/payments/{id}")
    assert error.body == {"errors": [{"error_code": 404}]}
    assert str(error) == "GoPay API error: HTTP 404"


def test_non_json_error_body_is_kept_as_text(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/payments/1", reply(400, text="Bad things"))
    with pytest.raises(GoPayHTTPError) as caught:
        client.get_payment_status("1")
    assert caught.value.body == "Bad things"


def test_invalid_json_success_body(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on(
        "GET",
        "/payments/1",
        httpx.Response(200, content=b"{", headers={"content-type": "application/json"}),
    )
    with pytest.raises(GoPaySDKError) as caught:
        client.get_payment_status("1")
    assert caught.value.error_code is ErrorCode.UNEXPECTED_RESPONSE


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(reply(200), id="empty"),
        pytest.param(reply(204), id="no-content"),
        pytest.param(reply(200, text="OK"), id="text"),
        pytest.param(httpx.Response(200, content=b"null"), id="null"),
        pytest.param(reply(200, ["not", "an", "object"]), id="list"),
        pytest.param(reply(200, "PAID"), id="string"),
    ],
)
def test_a_2xx_without_a_json_object_is_an_unexpected_response(
    client: GoPayClient, gateway: FakeGateway, errors: ErrorLog, answer: httpx.Response
) -> None:
    # The method is typed to return PaymentDetails; None or a list must not reach the caller.
    gateway.on("GET", "/payments/1", answer)
    with pytest.raises(GoPaySDKError) as caught:
        client.get_payment_status("1")
    assert caught.value.error_code is ErrorCode.UNEXPECTED_RESPONSE
    assert errors.errors == [caught.value]


def test_an_empty_answer_to_a_post_is_an_unexpected_response(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("POST", "/payments/1/refunds", reply(201))
    with pytest.raises(GoPaySDKError) as caught:
        client.refund_payment("1", {"amount": 1})
    assert caught.value.error_code is ErrorCode.UNEXPECTED_RESPONSE


@pytest.mark.parametrize(
    "body",
    [pytest.param({"id": "r-1"}, id="object"), pytest.param([{"id": "r-1"}, 7], id="scalar-item")],
)
def test_a_list_endpoint_needs_a_list_of_objects(
    client: GoPayClient, gateway: FakeGateway, body: object
) -> None:
    gateway.on("GET", "/payments/1/refunds", reply(200, body))
    with pytest.raises(GoPaySDKError) as caught:
        client.list_refunds("1")
    assert caught.value.error_code is ErrorCode.UNEXPECTED_RESPONSE


def test_an_empty_list_is_a_list(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/payments/1/refunds", reply(200, []))
    assert client.list_refunds("1") == []


def test_no_content_returns_none(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("DELETE", "/payments/1", reply(204))
    assert client.cancel_payment("1") is None


def test_a_delete_ignores_whatever_body_it_gets(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("DELETE", "/recurrences/rec-1", reply(200, text="Deleted"))
    assert client.stop_recurrence("rec-1") is None


def test_on_error_failure_does_not_replace_the_sdk_error(
    make_client: Callable[..., GoPayClient], gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(_error: GoPayError) -> None:
        raise RuntimeError("monitoring is down")

    client = make_client(on_error=broken)
    with pytest.raises(GoPaySDKError) as caught:
        client.get_payment_status("1")
    assert caught.value.error_code is ErrorCode.AUTH_TOKEN_MISSING
    assert "on_error callback raised" in caplog.text


def test_each_error_is_reported_once(
    client: GoPayClient, gateway: FakeGateway, errors: ErrorLog
) -> None:
    gateway.on("GET", "/payments/1", reply(404))
    for _ in range(2):
        with pytest.raises(GoPayHTTPError):
            client.get_payment_status("1")
    assert len(errors.errors) == 2
    assert errors.errors[0] is not errors.errors[1]


def test_delete_is_not_retried(client: GoPayClient, gateway: FakeGateway) -> None:
    # A cancel the API carried out before the 502 would answer 409 to a retry.
    gateway.on("DELETE", "/payments/1", reply(502))
    with pytest.raises(GoPayHTTPError) as caught:
        client.cancel_payment("1")
    assert caught.value.status == 502
    assert len(gateway.api_requests()) == 1


def test_problem_json_error_body_is_parsed(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on(
        "GET",
        "/payments/1",
        httpx.Response(
            400,
            content=b'{"title": "Bad"}',
            headers={"content-type": "application/problem+json; charset=utf-8"},
        ),
    )
    with pytest.raises(GoPayHTTPError) as caught:
        client.get_payment_status("1")
    assert caught.value.body == {"title": "Bad"}
