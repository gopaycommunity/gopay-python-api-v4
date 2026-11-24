"""Events and webhooks against a live gateway.

Delivery, retries and every event type are the gateway's to test; this checks that the SDK
reads what the gateway writes, and that a webhook it delivered verifies against its live keys.
"""

from __future__ import annotations

import base64
import os
import time
from itertools import islice
from typing import Any, Callable, Optional, TypeVar, cast

import httpx
import pytest

from gopay_payments import GoPayClient, GoPayEvent, GoPayHTTPError, is_known_event
from gopay_payments.types import PaymentCreateRequest

pytestmark = pytest.mark.e2e

T = TypeVar("T")

# demo-site's webhook receiver on the environment under test (its /api/v1/test/webhooks).
INSPECTION_URL = os.environ.get("GOPAY_E2E_WEBHOOK_INSPECTION_URL", "").strip()


def wait_for(what: str, probe: Callable[[], Optional[T]], timeout: float) -> T:
    """Poll ``probe`` every half second until it finds something; every wait is bounded."""
    deadline = time.monotonic() + timeout
    while True:
        found = probe()
        if found is not None:
            return found
        if time.monotonic() > deadline:
            pytest.fail(f"{what} did not appear within {timeout:g} s")
        time.sleep(0.5)


@pytest.fixture(scope="module")
def canceled(
    events_client: GoPayClient,
    goid: str,
    payment_request_factory: Callable[[], PaymentCreateRequest],
) -> tuple[str, GoPayEvent]:
    """A payment created and canceled here, and its payments.canceled event."""
    payment = events_client.create_payment(goid, payment_request_factory())
    payment_id = str(payment["id"])
    events_client.cancel_payment(payment_id)

    def find() -> Optional[GoPayEvent]:
        page = events_client.list_events(
            goid, {"related_type": "payments", "related_id": payment_id}
        )
        found: Optional[GoPayEvent] = next(
            (e for e in page["items"] if e["type"] == "payments.canceled"), None
        )
        return found

    # Recorded asynchronously, about a second after the cancel.
    # cast: mypy joins the GoPayEvent union when it solves wait_for's type variable.
    return payment_id, cast(GoPayEvent, wait_for("payments.canceled in list_events", find, 15))


def test_the_webhook_signing_keys_load(events_client: GoPayClient) -> None:
    keys = events_client.get_webhook_public_keys()
    assert keys["keys"]
    assert all(key.get("alg") == "RS256" and key.get("kid") for key in keys["keys"])


def test_the_cancel_of_a_payment_is_listed_as_payments_canceled(
    canceled: tuple[str, GoPayEvent],
) -> None:
    payment_id, event = canceled
    assert is_known_event(event)
    assert event["data"].get("payment_id") == payment_id


def test_get_event_returns_the_listed_event(
    events_client: GoPayClient, goid: str, canceled: tuple[str, GoPayEvent]
) -> None:
    _, event = canceled
    assert events_client.get_event(goid, event["id"]) == event


def test_iter_events_pages_newest_first_without_repeating_an_event(
    events_client: GoPayClient, goid: str, canceled: tuple[str, GoPayEvent]
) -> None:
    first = events_client.list_events(goid, {"max_items": 2})
    if "next_page_url" not in first:
        pytest.skip("the eshop has two events or fewer in the last 30 days: no page to cross")
    seen = list(islice(events_client.iter_events(goid, {"max_items": 2}), 5))
    ids = [event["id"] for event in seen]
    assert len(ids) > 2  # past the first page
    assert len(set(ids)) == len(ids)
    # Newest first by id (a ULID): occurred_at of events recorded within the same few
    # milliseconds can be out of that order.
    assert ids == sorted(ids, reverse=True)


def test_a_refused_filter_is_an_http_400(events_client: GoPayClient, goid: str) -> None:
    with pytest.raises(GoPayHTTPError) as caught:
        events_client.list_events(goid, {"max_items": 0})
    assert caught.value.status == 400
    assert caught.value.trace_id


def test_an_unknown_event_is_an_http_404(events_client: GoPayClient, goid: str) -> None:
    with pytest.raises(GoPayHTTPError) as caught:
        events_client.get_event(goid, "01AAAAAAAAAAAAAAAAAAAAAAAA")
    assert caught.value.status == 404


@pytest.mark.skipif(
    not INSPECTION_URL,
    reason="GOPAY_E2E_WEBHOOK_INSPECTION_URL is not set: no receiver to read a delivery back from",
)
def test_the_delivered_webhook_verifies_against_the_live_keys(
    events_client: GoPayClient, canceled: tuple[str, GoPayEvent]
) -> None:
    payment_id, event = canceled

    def delivered() -> Optional[dict[str, Any]]:
        response = httpx.get(INSPECTION_URL, params={"payment_id": payment_id}, timeout=15)
        response.raise_for_status()
        webhooks: list[dict[str, Any]] = response.json()["webhooks"]
        return next((w for w in webhooks if w.get("type") == "payments.canceled"), None)

    stored = wait_for("the payments.canceled webhook at the receiver", delivered, 30)
    assert stored["userAgent"] == "GoPay-Webhooks/1.0"
    verified = events_client.verify_webhook(
        base64.b64decode(stored["bodyBase64"]), stored["signature"]
    )
    assert verified == event
