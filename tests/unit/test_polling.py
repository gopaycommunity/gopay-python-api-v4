from __future__ import annotations

import threading
from typing import Any

import pytest

from gopay_payments import ErrorCode, GoPayClient, GoPayHTTPError, GoPaySDKError

from .conftest import ErrorLog, FakeGateway, reply

CHARGE = "/payments/42/charge"


def charge(state: str, redirect: str = "") -> Any:
    body: dict[str, Any] = {"id": "42", "state": state, "return_url": "https://shop.example/r"}
    if redirect:
        body["action"] = {"action_type": "EMV3DS", "redirect_url": redirect}
    return reply(200, body)


def test_charge_returns_on_succeeded_and_reports_each_redirect_once(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on(
        "GET",
        CHARGE,
        charge("PROCESSING"),
        charge("ACTION_REQUIRED", "https://acs.example/a"),
        charge("ACTION_REQUIRED", "https://acs.example/a"),
        charge("SUCCEEDED"),
    )
    redirects: list[str] = []
    states: list[str] = []
    result = client.await_charge_state(
        "42",
        on_action_required=redirects.append,
        on_state_change=lambda s: states.append(s["state"]),
        interval=1,
    )
    assert result["state"] == "SUCCEEDED"
    assert redirects == ["https://acs.example/a"]
    assert states == ["PROCESSING", "ACTION_REQUIRED", "ACTION_REQUIRED", "SUCCEEDED"]


def test_charge_failed_raises_with_the_state(
    client: GoPayClient, gateway: FakeGateway, errors: ErrorLog
) -> None:
    gateway.on(
        "GET",
        CHARGE,
        charge("PROCESSING"),
        reply(200, {"id": "42", "state": "FAILED", "fail_reason": "DECLINED"}),
    )
    with pytest.raises(GoPaySDKError) as caught:
        client.await_charge_state("42", interval=1)
    assert caught.value.error_code is ErrorCode.CHARGE_FAILED
    assert caught.value.charge_state["fail_reason"] == "DECLINED"
    assert errors.errors == [caught.value]


def test_charge_times_out_while_still_processing(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", CHARGE, charge("PROCESSING"))
    with pytest.raises(GoPaySDKError) as caught:
        client.await_charge_state("42", interval=1, initial_timeout=0)
    assert caught.value.error_code is ErrorCode.CHARGE_TIMEOUT


def test_action_required_lifts_the_initial_timeout(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on(
        "GET",
        CHARGE,
        charge("ACTION_REQUIRED", "https://acs.example/a"),
        charge("ACTION_REQUIRED", "https://acs.example/a"),
        charge("ACTION_REQUIRED", "https://acs.example/b"),
        charge("SUCCEEDED"),
    )
    redirects: list[str] = []
    result = client.await_charge_state(
        "42", on_action_required=redirects.append, interval=1, initial_timeout=0
    )
    assert result["state"] == "SUCCEEDED"
    assert redirects == ["https://acs.example/a", "https://acs.example/b"]


def test_charge_polling_can_be_cancelled(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", CHARGE, charge("PROCESSING"))
    cancel = threading.Event()
    with pytest.raises(GoPaySDKError) as caught:
        client.await_charge_state(
            "42", on_state_change=lambda _s: cancel.set(), cancel=cancel, interval=5
        )
    assert caught.value.error_code is ErrorCode.CHARGE_FAILED
    assert len(gateway.api_requests()) == 1


def test_cancelled_before_start_sends_nothing(client: GoPayClient, gateway: FakeGateway) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(GoPaySDKError):
        client.await_charge_state("42", cancel=cancel)
    assert gateway.api_requests() == []


def test_a_failing_poll_ends_polling_with_its_error(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", CHARGE, reply(404, {"errors": []}))
    with pytest.raises(GoPayHTTPError):
        client.await_charge_state("42", interval=1)


@pytest.mark.parametrize(
    "bad",
    [
        {"interval": -1},
        {"interval": 0},
        {"interval": 0.5},
        {"initial_timeout": "30"},
        {"interval": True},
    ],
)
def test_polling_arguments_are_validated(client: GoPayClient, bad: dict[str, Any]) -> None:
    with pytest.raises(GoPaySDKError) as caught:
        client.await_charge_state("42", **bad)
    assert caught.value.error_code is ErrorCode.INVALID_ARGUMENT


def test_payment_status_waits_for_a_terminal_state(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on(
        "GET",
        "/payments/42",
        reply(200, {"id": 42, "state": "CREATED"}),
        reply(200, {"id": 42, "state": "PAYMENT_METHOD_CHOSEN"}),
        reply(200, {"id": 42, "state": "PAID"}),
    )
    assert client.await_payment_status("42", interval=1)["state"] == "PAID"
    assert len(gateway.api_requests()) == 3


def test_payment_status_custom_terminal_states(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/payments/42", reply(200, {"id": 42, "state": "AUTHORIZED"}))
    result = client.await_payment_status("42", terminal_states={"AUTHORIZED"}, interval=1)
    assert result["state"] == "AUTHORIZED"


def test_payment_status_timeout(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/payments/42", reply(200, {"id": 42, "state": "CREATED"}))
    with pytest.raises(GoPaySDKError) as caught:
        client.await_payment_status("42", interval=1, timeout=0)
    assert caught.value.error_code is ErrorCode.CHARGE_TIMEOUT


def test_refund_failed_is_returned_not_raised(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on(
        "GET",
        "/refunds/r-1",
        reply(200, {"id": "r-1", "state": "REQUESTED"}),
        reply(200, {"id": "r-1", "state": "FAILED"}),
    )
    assert client.await_refund_state("r-1", interval=1)["state"] == "FAILED"


def test_recurrence_settles_on_started(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on(
        "GET",
        "/recurrences/rec-1",
        reply(200, {"id": "rec-1", "state": "REQUESTED", "payment": {"state": "CREATED"}}),
        reply(200, {"id": "rec-1", "state": "STARTED"}),
    )
    assert client.await_recurrence_state("rec-1", interval=1)["state"] == "STARTED"


def test_recurrence_still_new_cannot_settle(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/recurrences/rec-1", reply(200, {"id": "rec-1", "state": "NEW"}))
    with pytest.raises(GoPaySDKError) as caught:
        client.await_recurrence_state("rec-1", interval=1)
    assert caught.value.error_code is ErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize("payment_state", ["CANCELED", "TIMEOUTED"])
def test_recurrence_with_an_unpaid_finished_payment_cannot_settle(
    client: GoPayClient, gateway: FakeGateway, payment_state: str
) -> None:
    gateway.on(
        "GET",
        "/recurrences/rec-1",
        reply(200, {"id": "rec-1", "state": "REQUESTED", "payment": {"state": payment_state}}),
    )
    with pytest.raises(GoPaySDKError) as caught:
        client.await_recurrence_state("rec-1", interval=1)
    assert caught.value.error_code is ErrorCode.CHARGE_FAILED


def test_custom_recurrence_terminal_states_skip_the_reachability_check(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", "/recurrences/rec-1", reply(200, {"id": "rec-1", "state": "NEW"}))
    result = client.await_recurrence_state("rec-1", terminal_states=["NEW"], interval=1)
    assert result["state"] == "NEW"
