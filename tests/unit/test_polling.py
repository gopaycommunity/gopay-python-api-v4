from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from gopay_payments import ErrorCode, GoPayClient, GoPayHTTPError, GoPaySDKError, _polling

from .conftest import REAL_CLOCK, REAL_PAUSE, ErrorLog, FakeGateway, PollClock, reply

CHARGE = "/payments/42/charge"


def charge(state: str, redirect: str = "", emv3ds: str = "CHALLENGE_REQUIRED") -> Any:
    body: dict[str, Any] = {"id": "42", "state": state, "return_url": "https://shop.example/r"}
    if redirect:
        body["action"] = {"action_type": "EMV3DS", "state": emv3ds, "redirect_url": redirect}
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


@pytest.mark.parametrize("emv3ds", ["CREATED", "CHALLENGE_REQUIRED"])
def test_the_redirect_is_handed_out_while_the_customer_has_to_act(
    client: GoPayClient, gateway: FakeGateway, emv3ds: str
) -> None:
    gateway.on(
        "GET",
        CHARGE,
        charge("ACTION_REQUIRED", "https://acs.example/a", emv3ds=emv3ds),
        charge("SUCCEEDED"),
    )
    redirects: list[str] = []
    client.await_charge_state("42", on_action_required=redirects.append, interval=1)
    assert redirects == ["https://acs.example/a"]


FINISHED_3DS = [
    "AUTHENTICATED_CHALLENGE",
    "AUTHENTICATED_FRICTIONLESS",
    "NOT_AUTHENTICATED",
    "FAILED",
]


@pytest.mark.parametrize("emv3ds", FINISHED_3DS)
def test_a_finished_3ds_step_is_not_handed_out(
    client: GoPayClient, gateway: FakeGateway, emv3ds: str
) -> None:
    # Polling from the return URL: the customer is back, the URL leads to a closed session.
    gateway.on(
        "GET",
        CHARGE,
        charge("ACTION_REQUIRED", "https://acs.example/a", emv3ds=emv3ds),
        charge("SUCCEEDED"),
    )
    redirects: list[str] = []
    result = client.await_charge_state(
        "42", on_action_required=redirects.append, interval=1, initial_timeout=30
    )
    assert result["state"] == "SUCCEEDED"
    assert redirects == []


@pytest.mark.parametrize("emv3ds", FINISHED_3DS)
def test_a_finished_3ds_step_keeps_the_initial_timeout(
    client: GoPayClient, gateway: FakeGateway, emv3ds: str
) -> None:
    gateway.on("GET", CHARGE, charge("ACTION_REQUIRED", "https://acs.example/a", emv3ds=emv3ds))
    with pytest.raises(GoPaySDKError) as caught:
        client.await_charge_state("42", interval=1, initial_timeout=5)
    assert caught.value.error_code is ErrorCode.CHARGE_TIMEOUT


def test_action_required_without_a_redirect_keeps_the_initial_timeout(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", CHARGE, charge("ACTION_REQUIRED"))
    with pytest.raises(GoPaySDKError) as caught:
        client.await_charge_state("42", interval=1, initial_timeout=5)
    assert caught.value.error_code is ErrorCode.CHARGE_TIMEOUT


def test_charge_polling_can_be_cancelled(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", CHARGE, charge("PROCESSING"))
    cancel = threading.Event()
    with pytest.raises(GoPaySDKError) as caught:
        client.await_charge_state(
            "42", on_state_change=lambda _s: cancel.set(), cancel=cancel, interval=5
        )
    assert caught.value.error_code is ErrorCode.CHARGE_FAILED
    assert len(gateway.api_requests()) == 1


def test_cancel_cuts_a_real_pause_short(
    client: GoPayClient, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The autouse fixture stubs the pause; this test runs the real one, on real time.
    monkeypatch.setattr(_polling, "_pause", REAL_PAUSE)
    monkeypatch.setattr(_polling, "_clock", REAL_CLOCK)
    gateway.on("GET", CHARGE, charge("PROCESSING"))
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        # A pause that ignored the event would sleep out the interval and end in
        # CHARGE_TIMEOUT after three seconds, not hang.
        with pytest.raises(GoPaySDKError) as caught:
            client.await_charge_state("42", interval=2, initial_timeout=3, cancel=cancel)
    finally:
        timer.cancel()
    assert caught.value.error_code is ErrorCode.CHARGE_FAILED
    assert time.monotonic() - started < 1.5
    assert len(gateway.api_requests()) == 1


def test_real_pause_sleeps_and_reports_no_cancel() -> None:
    started = time.monotonic()
    assert REAL_PAUSE(0.05, None) is False
    assert REAL_PAUSE(0.05, threading.Event()) is False
    assert time.monotonic() - started >= 0.1


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
        {"interval": float("nan")},
        {"interval": float("inf")},
        {"initial_timeout": float("nan")},
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


def test_payment_status_stops_on_authorized(client: GoPayClient, gateway: FakeGateway) -> None:
    # A pre-authorized payment waits for the merchant's capture; polling cannot end that.
    gateway.on(
        "GET",
        "/payments/42",
        reply(200, {"id": 42, "state": "CREATED"}),
        reply(200, {"id": 42, "state": "AUTHORIZED"}),
    )
    assert client.await_payment_status("42", interval=1, timeout=30)["state"] == "AUTHORIZED"


def test_payment_status_custom_terminal_states(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", "/payments/42", reply(200, {"id": 42, "state": "PAYMENT_METHOD_CHOSEN"}))
    result = client.await_payment_status(
        "42", terminal_states={"PAYMENT_METHOD_CHOSEN"}, interval=1, timeout=30
    )
    assert result["state"] == "PAYMENT_METHOD_CHOSEN"


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
        # timeout: without the NEW check this would poll forever; with it, a regression
        # ends in CHARGE_TIMEOUT and fails the assert instead of hanging the run.
        client.await_recurrence_state("rec-1", interval=1, timeout=10)
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
        client.await_recurrence_state("rec-1", interval=1, timeout=10)
    assert caught.value.error_code is ErrorCode.CHARGE_FAILED


def test_custom_recurrence_terminal_states_skip_the_reachability_check(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", "/recurrences/rec-1", reply(200, {"id": "rec-1", "state": "NEW"}))
    result = client.await_recurrence_state("rec-1", terminal_states=["NEW"], interval=1)
    assert result["state"] == "NEW"


def test_charge_deadline_falls_between_polls(
    client: GoPayClient, gateway: FakeGateway, poll_clock: PollClock
) -> None:
    gateway.on("GET", CHARGE, charge("PROCESSING"))
    with pytest.raises(GoPaySDKError) as caught:
        client.await_charge_state("42", interval=2, initial_timeout=5)
    assert caught.value.error_code is ErrorCode.CHARGE_TIMEOUT
    # Polls at t=0, 2, 4; the last pause is cut to the remaining second.
    assert poll_clock.pauses == [2, 2, 1]
    assert len(gateway.api_requests()) == 3


def test_action_required_mid_wait_lifts_the_deadline(
    client: GoPayClient, gateway: FakeGateway, poll_clock: PollClock
) -> None:
    gateway.on(
        "GET",
        CHARGE,
        charge("PROCESSING"),
        charge("PROCESSING"),
        charge("ACTION_REQUIRED", "https://acs.example/a"),
        charge("ACTION_REQUIRED", "https://acs.example/a"),
        charge("ACTION_REQUIRED", "https://acs.example/a"),
        charge("SUCCEEDED"),
    )
    result = client.await_charge_state("42", interval=2, initial_timeout=5)
    assert result["state"] == "SUCCEEDED"
    assert poll_clock.now - 1000.0 > 5  # ran past the initial timeout


def test_status_timeout_falls_between_polls(
    client: GoPayClient, gateway: FakeGateway, poll_clock: PollClock
) -> None:
    gateway.on("GET", "/payments/42", reply(200, {"id": 42, "state": "CREATED"}))
    with pytest.raises(GoPaySDKError) as caught:
        client.await_payment_status("42", interval=3, timeout=7)
    assert caught.value.error_code is ErrorCode.CHARGE_TIMEOUT
    assert poll_clock.pauses == [3, 3, 1]


@pytest.mark.parametrize("states", ["PAID", ["PAID", 1]])
def test_terminal_states_must_be_a_collection_of_strings(
    client: GoPayClient, gateway: FakeGateway, states: Any
) -> None:
    with pytest.raises(GoPaySDKError) as caught:
        client.await_payment_status("42", terminal_states=states)
    assert caught.value.error_code is ErrorCode.INVALID_ARGUMENT
    assert gateway.api_requests() == []


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -1])
def test_timeouts_must_be_finite(client: GoPayClient, timeout: float) -> None:
    with pytest.raises(GoPaySDKError) as caught:
        client.await_refund_state("r-1", timeout=timeout)
    assert caught.value.error_code is ErrorCode.INVALID_ARGUMENT
