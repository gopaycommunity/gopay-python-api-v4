from __future__ import annotations

from typing import Any, Callable

import pytest

from gopay_payments import ErrorCode, GoPayClient, GoPaySDKError
from gopay_payments.types import (
    BrowserData,
    LinkCreateRequest,
    PaymentChargeInput,
    PaymentCreateRequest,
    RecurrenceCreateRequest,
)

from .conftest import ErrorLog, FakeGateway, json_of, path_of, reply

PAYMENT: PaymentCreateRequest = {
    "amount": 100,
    "currency": "CZK",
    "order_number": "001",
    "customer": {"email": "buyer@example.com"},
    "callback": {
        "return_url": "https://shop.example/return",
        "notification_url": "https://shop.example/notify",
    },
}
BROWSER_DATA: BrowserData = {
    "accept_header": "text/html",
    "color_depth": 24,
    "ip": "192.0.2.10",
    "javascript_enabled": True,
    "language": "cs-CZ",
    "screen_height": 1080,
    "screen_width": 1920,
    "timezone": -60,
    "user_agent": "Mozilla/5.0",
}
CHARGE: PaymentChargeInput = {
    "payment_instrument": {
        "payment_instrument": "PAYMENT_CARD",
        "input": {"input_type": "CARD_TOKEN", "card_token": "tok"},
        "browser_data": BROWSER_DATA,
    }
}
LINK: LinkCreateRequest = {"payment": PAYMENT, "reusable": False}
RECURRENCE: RecurrenceCreateRequest = {
    "type": "ON_DEMAND",
    "recurrence_date_to": "2030-12-31",
    "payment": PAYMENT,
}
GOID = "8123456789"
VALIDATION = {"validationUrl": "https://apple.example/v"}

Call = Callable[[GoPayClient], Any]

# (name, call, method, path, body) — the whole server surface of the spec.
CASES: list[tuple[str, Call, str, str, Any]] = [
    (
        "create_payment",
        lambda c: c.create_payment(GOID, PAYMENT),
        "POST",
        f"/eshops/{GOID}/payments",
        PAYMENT,
    ),
    (
        "create_payment int goid",
        lambda c: c.create_payment(int(GOID), PAYMENT),
        "POST",
        f"/eshops/{GOID}/payments",
        PAYMENT,
    ),
    ("get_payment_status", lambda c: c.get_payment_status("42"), "GET", "/payments/42", None),
    ("cancel_payment", lambda c: c.cancel_payment("42"), "DELETE", "/payments/42", None),
    (
        "charge_payment",
        lambda c: c.charge_payment("42", CHARGE),
        "POST",
        "/payments/42/charge",
        CHARGE,
    ),
    ("get_charge_state", lambda c: c.get_charge_state("42"), "GET", "/payments/42/charge", None),
    (
        "get_google_pay_info",
        lambda c: c.get_google_pay_info("42"),
        "GET",
        "/payments/42/google-pay/info",
        None,
    ),
    (
        "get_apple_pay_info",
        lambda c: c.get_apple_pay_info("42"),
        "GET",
        "/payments/42/apple-pay/info",
        None,
    ),
    (
        "validate_apple_pay_merchant",
        lambda c: c.validate_apple_pay_merchant("42", {"validationUrl": "https://apple.example/v"}),
        "POST",
        "/payments/42/apple-pay/validate",
        VALIDATION,
    ),
    (
        "get_qr_payment_info",
        lambda c: c.get_qr_payment_info("42"),
        "GET",
        "/payments/42/qr-payment/info",
        None,
    ),
    (
        "get_qr_payment_info svg",
        lambda c: c.get_qr_payment_info("42", "svg"),
        "GET",
        "/payments/42/qr-payment/info?format=svg",
        None,
    ),
    (
        "refund_payment",
        lambda c: c.refund_payment("42", {"amount": 50}),
        "POST",
        "/payments/42/refunds",
        {"amount": 50},
    ),
    ("list_refunds", lambda c: c.list_refunds("42"), "GET", "/payments/42/refunds", None),
    ("get_refund", lambda c: c.get_refund("r-1"), "GET", "/refunds/r-1", None),
    ("get_card_details", lambda c: c.get_card_details("c-1"), "GET", "/cards/tokens/c-1", None),
    ("delete_card", lambda c: c.delete_card("c-1"), "DELETE", "/cards/tokens/c-1", None),
    (
        "tokenize_encrypted_card",
        lambda c: c.tokenize_encrypted_card("eyJ.jwe"),
        "POST",
        "/cards/tokens",
        {"payload": "eyJ.jwe"},
    ),
    (
        "create_payment_link",
        lambda c: c.create_payment_link(GOID, LINK),
        "POST",
        f"/eshops/{GOID}/links",
        LINK,
    ),
    (
        "get_payment_link",
        lambda c: c.get_payment_link(GOID, "l-1"),
        "GET",
        f"/eshops/{GOID}/links/l-1",
        None,
    ),
    (
        "disable_payment_link",
        lambda c: c.disable_payment_link(GOID, "l-1"),
        "DELETE",
        f"/eshops/{GOID}/links/l-1",
        None,
    ),
    (
        "create_recurrence",
        lambda c: c.create_recurrence(GOID, RECURRENCE),
        "POST",
        f"/eshops/{GOID}/recurrences",
        RECURRENCE,
    ),
    ("get_recurrence", lambda c: c.get_recurrence("rec-1"), "GET", "/recurrences/rec-1", None),
    (
        "start_recurrence",
        lambda c: c.start_recurrence("rec-1", {"amount": 200}),
        "POST",
        "/recurrences/rec-1/start",
        {"amount": 200},
    ),
    (
        "create_next_payment",
        lambda c: c.create_next_payment("rec-1"),
        "POST",
        "/recurrences/rec-1/next",
        None,
    ),
    ("stop_recurrence", lambda c: c.stop_recurrence("rec-1"), "DELETE", "/recurrences/rec-1", None),
]


@pytest.mark.parametrize(
    ("name", "call", "method", "path", "body"), CASES, ids=[c[0] for c in CASES]
)
def test_endpoint(
    client: GoPayClient,
    gateway: FakeGateway,
    name: str,
    call: Callable[[GoPayClient], Any],
    method: str,
    path: str,
    body: Any,
) -> None:
    gateway.on(method, path, reply(204) if method == "DELETE" else reply(200, {"ok": name}))
    result = call(client)
    request = gateway.last()
    assert (request.method, path_of(request)) == (method, path)
    assert json_of(request) == body
    assert result == (None if method == "DELETE" else {"ok": name})


def test_apple_pay_validation_sends_the_origin(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("POST", "/payments/42/apple-pay/validate", reply(200, {}))
    client.validate_apple_pay_merchant(
        "42", {"validationUrl": "https://apple.example/v"}, origin="https://shop.example"
    )
    assert gateway.last().headers["origin"] == "https://shop.example"


@pytest.mark.parametrize(
    "origin",
    [
        "http://shop.example",
        "https://shop.example/checkout",
        "shop.example",
        "https://u@shop.example",
    ],
)
def test_apple_pay_origin_must_be_a_bare_https_origin(
    client: GoPayClient, gateway: FakeGateway, origin: str
) -> None:
    with pytest.raises(GoPaySDKError) as caught:
        client.validate_apple_pay_merchant("42", origin=origin)
    assert caught.value.error_code is ErrorCode.INVALID_ARGUMENT
    assert gateway.api_requests() == []


def test_qr_format_is_validated(
    client: GoPayClient, gateway: FakeGateway, errors: ErrorLog
) -> None:
    with pytest.raises(GoPaySDKError) as caught:
        client.get_qr_payment_info("42", "gif")  # type: ignore[arg-type]
    assert caught.value.error_code is ErrorCode.INVALID_ARGUMENT
    assert errors.errors == [caught.value]
    assert gateway.api_requests() == []


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        ("a b", "a%20b"),
        ("../recurrences/1", "..%2Frecurrences%2F1"),
        ("pay(1)!*'", "pay%281%29%21%2A%27"),
        ("~id_.-", "~id_.-"),
        ("  42  ", "42"),
    ],
)
def test_path_segments_are_encoded(
    client: GoPayClient, gateway: FakeGateway, value: str, encoded: str
) -> None:
    gateway.on("GET", f"/payments/{encoded}", reply(200, {}))
    client.get_payment_status(value)
    assert path_of(gateway.last()) == f"/payments/{encoded}"


@pytest.mark.parametrize("value", ["", "   ", ".", "..", None, 42, True])
def test_invalid_path_segments_are_refused(
    client: GoPayClient, gateway: FakeGateway, errors: ErrorLog, value: Any
) -> None:
    with pytest.raises(GoPaySDKError) as caught:
        client.get_payment_status(value)
    assert caught.value.error_code is ErrorCode.INVALID_ARGUMENT
    assert errors.errors == [caught.value]
    assert gateway.api_requests() == []


def test_bool_is_not_a_goid(client: GoPayClient) -> None:
    with pytest.raises(GoPaySDKError):
        client.create_payment(True, PAYMENT)


def test_tokenize_refuses_an_empty_payload(client: GoPayClient) -> None:
    with pytest.raises(GoPaySDKError) as caught:
        client.tokenize_encrypted_card(" ")
    assert caught.value.error_code is ErrorCode.INVALID_ARGUMENT
