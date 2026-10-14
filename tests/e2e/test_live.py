from __future__ import annotations

import pytest

from gopay_payments import ErrorCode, GoPayClient, GoPayHTTPError, GoPaySDKError
from gopay_payments.types import PaymentCreateRequest

pytestmark = pytest.mark.e2e

# Every wait is bounded: a live test must never poll an environment indefinitely.
INTERVAL = 2.0
TIMEOUT = 20.0


def test_payment_lifecycle(
    client: GoPayClient, goid: str, payment_request: PaymentCreateRequest
) -> None:
    payment = client.create_payment(goid, payment_request)
    assert payment["state"] == "CREATED"
    assert payment["gw_url"].startswith("https://")
    pid = str(payment["id"])

    assert client.get_payment_status(pid)["order_number"] == payment_request["order_number"]
    assert client.list_refunds(pid) == []

    qr = client.get_qr_payment_info(pid)
    assert qr["amount"] == payment_request["amount"]

    with pytest.raises(GoPayHTTPError) as no_charge:
        client.get_charge_state(pid)
    assert no_charge.value.status == 404
    assert no_charge.value.endpoint == "/payments/{id}/charge"

    client.cancel_payment(pid)
    assert (
        client.await_payment_status(pid, interval=INTERVAL, timeout=TIMEOUT)["state"] == "CANCELED"
    )

    with pytest.raises(GoPayHTTPError) as conflict:
        client.cancel_payment(pid)
    assert conflict.value.status == 409


def test_payment_link_lifecycle(
    client: GoPayClient, goid: str, payment_request: PaymentCreateRequest
) -> None:
    link = client.create_payment_link(
        goid, {"payment": payment_request, "reusable": False, "expires_in": 3600}
    )
    assert link["active"] is True
    assert link["url"].startswith("https://")
    lid = str(link["id"])

    assert client.get_payment_link(goid, lid)["id"] == link["id"]
    client.disable_payment_link(goid, lid)
    disabled = client.get_payment_link(goid, lid)
    assert disabled["active"] is False
    assert disabled.get("stop_reason") == "FROM_API"


def test_recurrence_lifecycle(
    client: GoPayClient, goid: str, payment_request: PaymentCreateRequest
) -> None:
    rec = client.create_recurrence(
        goid, {"type": "ON_DEMAND", "recurrence_date_to": "2030-12-31", "payment": payment_request}
    )
    rid = str(rec["id"])
    assert rec["state"] == "NEW"

    with pytest.raises(GoPaySDKError) as still_new:
        client.await_recurrence_state(rid, interval=INTERVAL, timeout=TIMEOUT)
    assert still_new.value.error_code is ErrorCode.INVALID_ARGUMENT

    first = client.start_recurrence(rid, {"order_description": "first payment"})
    assert client.get_recurrence(rid)["state"] == "REQUESTED"

    with pytest.raises(GoPayHTTPError) as not_started:
        client.create_next_payment(rid)
    assert not_started.value.status == 409

    # A recurrence waiting for its first payment cannot be stopped until that payment is
    # finished, and the wait for it to start then ends at once instead of polling on.
    client.cancel_payment(str(first["id"]))
    with pytest.raises(GoPaySDKError) as unpayable:
        client.await_recurrence_state(rid, interval=INTERVAL, timeout=TIMEOUT)
    assert unpayable.value.error_code is ErrorCode.CHARGE_FAILED

    client.stop_recurrence(rid)
    stopped = client.get_recurrence(rid)
    assert stopped["state"] == "STOPPED"
    assert stopped.get("stop_reason") == "CANCELLED_VIA_API"


def test_unknown_ids_map_to_http_404(client: GoPayClient) -> None:
    for call in (lambda: client.get_payment_status("1"), lambda: client.get_refund("1")):
        with pytest.raises(GoPayHTTPError) as caught:
            call()
        assert caught.value.status == 404
