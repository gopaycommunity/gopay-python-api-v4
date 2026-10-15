from __future__ import annotations

import base64
import threading
import time
from typing import Callable

import httpx
import pytest

from gopay_payments import (
    ErrorCode,
    GoPayClient,
    GoPayHTTPError,
    GoPaySDKError,
    InMemoryTokenCache,
    __version__,
)

from .conftest import TOKEN_PATH, ErrorLog, FakeGateway, form_of, reply

SCOPE = "payment:write payment:read"


def test_authenticate_requests_a_client_credentials_token(
    make_client: Callable[..., GoPayClient], gateway: FakeGateway
) -> None:
    client = make_client()
    client.authenticate("client-id", "client-secret", SCOPE)

    [request] = gateway.token_requests()
    assert request.method == "POST"
    expected = base64.b64encode(b"client-id:client-secret").decode()
    assert request.headers["authorization"] == f"Basic {expected}"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.headers["user-agent"] == f"GoPay Python v4 {__version__}"
    assert form_of(request) == {"grant_type": ["client_credentials"], "scope": [SCOPE]}
    assert client.is_authenticated()


def test_requests_carry_the_bearer_token(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/payments/1", reply(200, {"id": 1, "state": "CREATED"}))
    client.get_payment_status("1")
    assert gateway.last().headers["authorization"] == "Bearer token-1"


def test_a_shared_cache_spares_the_second_client_a_token_request(
    make_client: Callable[..., GoPayClient], gateway: FakeGateway
) -> None:
    cache = InMemoryTokenCache()
    make_client(token_cache=cache).authenticate("client-id", "client-secret", SCOPE)
    make_client(token_cache=cache).authenticate("client-id", "client-secret", SCOPE)
    assert len(gateway.token_requests()) == 1


def test_a_different_scope_gets_its_own_token(
    make_client: Callable[..., GoPayClient], gateway: FakeGateway
) -> None:
    cache = InMemoryTokenCache()
    make_client(token_cache=cache).authenticate("client-id", "client-secret", SCOPE)
    make_client(token_cache=cache).authenticate("client-id", "client-secret", "card:read")
    assert len(gateway.token_requests()) == 2


def test_cache_key_does_not_contain_the_secret(
    make_client: Callable[..., GoPayClient],
) -> None:
    cache = InMemoryTokenCache()
    make_client(token_cache=cache).authenticate("client-id", "s3cr3t-value", SCOPE)
    [key] = cache._tokens
    assert "s3cr3t-value" not in key
    assert "client-id" not in key


def test_rejected_credentials_raise_and_leave_the_client_unauthenticated(
    make_client: Callable[..., GoPayClient], gateway: FakeGateway, errors: ErrorLog
) -> None:
    gateway.on("POST", TOKEN_PATH, reply(401, {"errors": [{"error_code": 401}]}))
    client = make_client()
    with pytest.raises(GoPayHTTPError) as caught:
        client.authenticate("client-id", "wrong", SCOPE)
    assert caught.value.status == 401
    assert caught.value.endpoint == TOKEN_PATH
    assert errors.errors == [caught.value]
    assert not client.is_authenticated()
    with pytest.raises(GoPaySDKError) as missing:
        client.get_payment_status("1")
    assert missing.value.error_code is ErrorCode.AUTH_TOKEN_MISSING


@pytest.mark.parametrize(
    "body",
    [
        {"access_token": "t"},
        {"expires_in": 900},
        {"access_token": "", "expires_in": 900},
        {"access_token": "t", "expires_in": True},
        ["not", "an", "object"],
    ],
)
def test_invalid_token_response(
    make_client: Callable[..., GoPayClient], gateway: FakeGateway, body: object
) -> None:
    gateway.on("POST", TOKEN_PATH, reply(200, body))
    client = make_client()
    with pytest.raises(GoPaySDKError) as caught:
        client.authenticate("client-id", "client-secret", SCOPE)
    assert caught.value.error_code is ErrorCode.AUTH_INVALID_RESPONSE


@pytest.mark.parametrize("field", ["client_id", "client_secret", "scope"])
def test_authenticate_rejects_blank_arguments(
    make_client: Callable[..., GoPayClient], gateway: FakeGateway, field: str
) -> None:
    args = {"client_id": "id", "client_secret": "secret", "scope": SCOPE, field: " "}
    client = make_client()
    with pytest.raises(GoPaySDKError) as caught:
        client.authenticate(**args)
    assert caught.value.error_code is ErrorCode.INVALID_ARGUMENT
    assert gateway.requests == []


def test_calling_without_authenticate(
    make_client: Callable[..., GoPayClient], errors: ErrorLog
) -> None:
    client = make_client()
    with pytest.raises(GoPaySDKError) as caught:
        client.get_payment_status("1")
    assert caught.value.error_code is ErrorCode.AUTH_TOKEN_MISSING
    assert errors.errors == [caught.value]


def test_a_token_close_to_expiry_is_replaced_before_use(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", "/payments/1", reply(200, {"id": 1}))
    # 900 s token; 880 s later it is inside the 30 s refresh margin.
    client._transport._clock = lambda: time.time() + 880
    client.get_payment_status("1")
    assert len(gateway.token_requests()) == 2
    assert gateway.last().headers["authorization"] == "Bearer token-2"


def test_a_401_refreshes_the_token_and_retries_once(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", "/payments/1", reply(401), reply(200, {"id": 1, "state": "PAID"}))
    assert client.get_payment_status("1")["state"] == "PAID"
    first, second = gateway.api_requests()
    assert first.headers["authorization"] == "Bearer token-1"
    assert second.headers["authorization"] == "Bearer token-2"


def test_a_second_401_raises_but_keeps_the_credentials(
    client: GoPayClient, gateway: FakeGateway, errors: ErrorLog
) -> None:
    gateway.on("GET", "/cards/tokens/c-1", reply(401))
    gateway.on("GET", "/payments/1", reply(200, {"id": 1}))
    with pytest.raises(GoPaySDKError) as caught:
        client.get_card_details("c-1")
    assert caught.value.error_code is ErrorCode.AUTH_UNAUTHORIZED
    assert errors.errors == [caught.value]
    assert len(gateway.api_requests()) == 2
    # One endpoint refusing a fresh token says nothing about the others.
    assert client.get_payment_status("1")["id"] == 1  # type: ignore[comparison-overlap]
    assert gateway.last().headers["authorization"] == "Bearer token-3"


def test_a_failed_refresh_raises_auth_refresh_failed(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", "/payments/1", reply(401))
    gateway.on("POST", TOKEN_PATH, reply(400, {"errors": [{"error_code": 400}]}))
    with pytest.raises(GoPaySDKError) as caught:
        client.get_payment_status("1")
    assert caught.value.error_code is ErrorCode.AUTH_REFRESH_FAILED
    assert isinstance(caught.value.__cause__, GoPayHTTPError)
    assert caught.value.__cause__.status == 400


def test_concurrent_callers_share_one_refresh(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/payments/1", reply(200, {"id": 1}))
    tokens_issued = 0

    def slow_token(_request: httpx.Request) -> httpx.Response:
        nonlocal tokens_issued
        tokens_issued += 1
        time.sleep(0.05)
        return reply(200, {"access_token": f"shared-{tokens_issued}", "expires_in": 900})

    gateway.on("POST", TOKEN_PATH, slow_token)
    client._transport._clock = lambda: time.time() + 880  # every caller sees an expiring token

    start = threading.Barrier(8)

    def call() -> None:
        start.wait()
        client.get_payment_status("1")

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Eight callers found the token expiring at once; one refreshed and the others waited
    # for it and reused the result.
    assert tokens_issued == 1
    assert len(gateway.api_requests()) == 8
    assert {r.headers["authorization"] for r in gateway.api_requests()} == {"Bearer shared-1"}


def test_logout_forgets_token_and_credentials(client: GoPayClient) -> None:
    client.logout()
    assert not client.is_authenticated()
    with pytest.raises(GoPaySDKError) as caught:
        client.get_payment_status("1")
    assert caught.value.error_code is ErrorCode.AUTH_TOKEN_MISSING


def test_browser_keys(make_client: Callable[..., GoPayClient]) -> None:
    client = make_client(shareable_key="share-key")
    client.authenticate("client-id", "client-secret", SCOPE)
    assert client.get_browser_keys() == {"shareable_key": "share-key", "client_id": "client-id"}


def test_browser_keys_need_shareable_key_and_authentication(
    make_client: Callable[..., GoPayClient], errors: ErrorLog
) -> None:
    client = make_client()
    client.authenticate("client-id", "client-secret", SCOPE)
    with pytest.raises(GoPaySDKError) as caught:
        client.get_browser_keys()
    assert caught.value.error_code is ErrorCode.AUTH_CREDENTIALS_MISSING
    assert errors.errors == [caught.value]

    unauthenticated = make_client(shareable_key="share-key")
    with pytest.raises(GoPaySDKError):
        unauthenticated.get_browser_keys()
    unauthenticated.set_shareable_key("other")
    unauthenticated.authenticate("client-id", "client-secret", SCOPE)
    assert unauthenticated.get_browser_keys()["shareable_key"] == "other"


def test_a_transient_refresh_failure_keeps_the_credentials(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", "/payments/1", reply(200, {"id": 1}))
    gateway.on(
        "POST",
        TOKEN_PATH,
        reply(503),
        reply(200, {"access_token": "recovered", "expires_in": 900}),
    )
    client._transport._clock = lambda: time.time() + 880  # the token needs replacing
    with pytest.raises(GoPaySDKError) as caught:
        client.get_payment_status("1")
    assert caught.value.error_code is ErrorCode.AUTH_REFRESH_FAILED
    client.get_payment_status("1")
    assert gateway.last().headers["authorization"] == "Bearer recovered"


def test_rejected_credentials_on_refresh_are_forgotten(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("POST", TOKEN_PATH, reply(401))
    client._transport._clock = lambda: time.time() + 880
    with pytest.raises(GoPaySDKError):
        client.get_payment_status("1")
    with pytest.raises(GoPaySDKError) as caught:
        client.get_payment_status("1")
    assert caught.value.error_code is ErrorCode.AUTH_TOKEN_MISSING
