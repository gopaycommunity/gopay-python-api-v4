from __future__ import annotations

from typing import Callable

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


def test_retries_back_off(client: GoPayClient, gateway: FakeGateway) -> None:
    pauses: list[float] = []
    client._transport._sleep = pauses.append
    gateway.on("GET", "/payments/1", reply(500), reply(500), reply(200, {"id": 1}))
    client.get_payment_status("1")
    assert pauses == [0.2, 0.4]


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
    assert caught.value.error_code is ErrorCode.NETWORK_ERROR


def test_no_content_returns_none(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("DELETE", "/payments/1", reply(204))
    assert client.cancel_payment("1") is None


def test_extra_headers_cannot_replace_reserved_ones(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on(
        "POST", "/payments/1/apple-pay/validate", reply(200, {"merchantSessionIdentifier": "x"})
    )
    client._transport.request(
        "POST",
        "/payments/1/apple-pay/validate",
        headers={
            "Origin": "https://shop.example",
            "Authorization": "Bearer stolen",
            "user-agent": "x",
        },
    )
    request = gateway.last()
    assert request.headers["origin"] == "https://shop.example"
    assert request.headers["authorization"] == "Bearer token-1"
    assert request.headers["user-agent"].startswith("GoPay Python v4 ")


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
