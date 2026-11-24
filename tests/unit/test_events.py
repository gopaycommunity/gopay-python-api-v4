from __future__ import annotations

import datetime
from collections.abc import Iterable
from typing import Any, Callable

import pytest

from gopay_payments import (
    ErrorCode,
    GoPayClient,
    GoPayEvent,
    GoPayHTTPError,
    GoPayScopes,
    GoPaySDKError,
    combine_scopes,
)

from .conftest import BASE, ErrorLog, FakeGateway, path_of, reply

EVENTS = "/eshops/8123456789/events"
UTC = datetime.timezone.utc


def ev(n: int, type_: str = "payments.paid") -> dict[str, Any]:
    return {
        "id": f"evt-{n}",
        "type": type_,
        "occurred_at": "2026-07-08T12:04:39.123Z",
        "data": {"payment_id": str(n)},
    }


def page(*items: dict[str, Any], next_query: str | None = None, host: str = BASE) -> Any:
    body: dict[str, Any] = {"items": list(items)}
    if next_query is not None:
        body["next_page_url"] = f"{host}{EVENTS}?{next_query}"
    return reply(200, body)


def collect(events: Iterable[GoPayEvent], seen: list[str]) -> None:
    """Record each event id as it arrives, so a test sees what came before an error."""
    for event in events:
        seen.append(event["id"])


def event_paths(gateway: FakeGateway) -> list[str]:
    return [path_of(r) for r in gateway.api_requests()]


# ------------------------------------------------------------------ list_events


def test_list_events_sends_the_filters_rfc_3986_encoded(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    query = (
        "max_items=20&type=refunds.%2A%2Cpayments.%2A"
        "&occurred_after=2026-07-01T00%3A00%3A00.000%2B00%3A00"
        "&occurred_before=2026-08-01T00%3A00%3A00Z&related_type=payments&related_id=300000001"
    )
    gateway.on("GET", f"{EVENTS}?{query}", page(ev(1)))
    result = client.list_events(
        8123456789,
        {
            "max_items": 20,
            "type": "refunds.*,payments.*",
            "occurred_after": datetime.datetime(2026, 7, 1, tzinfo=UTC),
            "occurred_before": "2026-08-01T00:00:00Z",
            "related_type": "payments",
            "related_id": "300000001",
        },
    )
    assert result == {"items": [ev(1)]}
    assert event_paths(gateway) == [f"{EVENTS}?{query}"]


def test_no_params_send_no_query(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", EVENTS, page())
    assert client.list_events("8123456789") == {"items": []}
    assert client.list_events("8123456789", {"type": None}) == {"items": []}  # type: ignore[arg-type]
    assert event_paths(gateway) == [EVENTS, EVENTS]


def test_an_instant_keeps_its_milliseconds_and_its_offset(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    prague = datetime.timezone(datetime.timedelta(hours=2))
    instant = datetime.datetime(2026, 7, 1, 12, 30, 5, 123456, tzinfo=prague)
    gateway.on("GET", f"{EVENTS}?occurred_after=2026-07-01T12%3A30%3A05.123%2B02%3A00", page())
    client.list_events("8123456789", {"occurred_after": instant})
    assert gateway.last().url.params["occurred_after"] == "2026-07-01T12:30:05.123+02:00"


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"occurred_after": datetime.datetime(2026, 7, 1)}, id="naive datetime"),
        pytest.param({"max_items": True}, id="bool"),
        pytest.param({"type": ["payments.paid"]}, id="list"),
        pytest.param({"occurred_after": datetime.date(2026, 7, 1)}, id="date"),
        pytest.param("type=payments.paid", id="not a dict"),
    ],
)
def test_a_filter_that_cannot_be_sent_is_refused_before_any_request(
    client: GoPayClient, gateway: FakeGateway, errors: ErrorLog, params: Any
) -> None:
    with pytest.raises(GoPaySDKError) as caught:
        client.list_events("8123456789", params)
    assert caught.value.error_code is ErrorCode.INVALID_ARGUMENT
    assert errors.errors == [caught.value]
    assert gateway.api_requests() == []


def test_an_event_type_the_sdk_does_not_know_is_listed_as_sent(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", EVENTS, page(ev(1, "payments.something_new")))
    assert client.list_events("8123456789")["items"] == [ev(1, "payments.something_new")]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="no items"),
        pytest.param({"items": None}, id="items null"),
        pytest.param({"items": {"0": ev(1)}}, id="items an object"),
        pytest.param({"items": [ev(1), "evt-2"]}, id="an item not an object"),
        pytest.param({"items": [{**ev(1), "data": None}]}, id="an item without data"),
        pytest.param({"items": [], "next_page_url": 5}, id="cursor not a string"),
        pytest.param({"items": [], "next_page_url": f"{BASE}{EVENTS}"}, id="cursor without query"),
        pytest.param({"items": [], "next_page_url": f"{BASE}{EVENTS}?&="}, id="cursor empty query"),
        pytest.param([], id="a list"),
    ],
)
def test_a_page_the_sdk_cannot_read_is_an_unexpected_response_with_its_ids(
    client: GoPayClient, gateway: FakeGateway, body: Any
) -> None:
    gateway.on("GET", EVENTS, reply(200, body))
    with pytest.raises(GoPaySDKError) as caught:
        client.list_events("8123456789")
    assert caught.value.error_code is ErrorCode.UNEXPECTED_RESPONSE
    assert caught.value.trace_id == gateway.last().headers["x-gopay-trace-id"]


def test_an_empty_cursor_is_the_last_page(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", EVENTS, reply(200, {"items": [ev(1)], "next_page_url": ""}))
    assert [e["id"] for e in client.iter_events("8123456789")] == ["evt-1"]


# -------------------------------------------------------------------- get_event


def test_get_event_returns_the_event(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", f"{EVENTS}/evt-1", reply(200, ev(1)))
    assert client.get_event(8123456789, "evt-1") == ev(1)


def test_get_event_refuses_a_body_that_is_not_an_event(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", f"{EVENTS}/evt-1", reply(200, {"id": "evt-1"}))
    with pytest.raises(GoPaySDKError) as caught:
        client.get_event("8123456789", "evt-1")
    assert caught.value.error_code is ErrorCode.UNEXPECTED_RESPONSE


@pytest.mark.parametrize("event_id", ["", "..", None])
def test_get_event_validates_the_id(
    client: GoPayClient, gateway: FakeGateway, event_id: Any
) -> None:
    with pytest.raises(GoPaySDKError) as caught:
        client.get_event("8123456789", event_id)
    assert caught.value.error_code is ErrorCode.INVALID_ARGUMENT
    assert gateway.api_requests() == []


# ------------------------------------------------------------------ iter_events


def test_iter_events_fetches_each_page_only_when_the_previous_is_used_up(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on(
        "GET", f"{EVENTS}?max_items=2", page(ev(1), ev(2), next_query="max_items=2&prev_item=evt-2")
    )
    gateway.on("GET", f"{EVENTS}?max_items=2&prev_item=evt-2", page(ev(3)))
    events = client.iter_events("8123456789", {"max_items": 2})
    assert gateway.api_requests() == []  # nothing until the first iteration
    assert next(events)["id"] == "evt-1"
    assert next(events)["id"] == "evt-2"
    assert len(gateway.api_requests()) == 1
    assert [e["id"] for e in events] == ["evt-3"]
    assert len(gateway.api_requests()) == 2


def test_only_the_query_of_the_cursor_is_followed_never_its_host(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on(
        "GET",
        EVENTS,
        page(ev(1), next_query="prev_item=evt-1", host="https://attacker.example/other/prefix"),
    )
    gateway.on("GET", f"{EVENTS}?prev_item=evt-1", page(ev(2)))
    assert [e["id"] for e in client.iter_events("8123456789")] == ["evt-1", "evt-2"]
    assert all(r.url.host == "gw.sandbox.gopay.com" for r in gateway.requests)
    assert str(gateway.last().url) == f"{BASE}{EVENTS}?prev_item=evt-1"


def test_the_cursor_query_is_sent_as_the_gateway_wrote_it(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    cursor = "type=refunds.%2A&prev_item=evt-1&&max_items=1"
    gateway.on("GET", EVENTS, page(ev(1), next_query=cursor))
    gateway.on("GET", f"{EVENTS}?type=refunds.%2A&prev_item=evt-1&max_items=1", page())
    list(client.iter_events("8123456789"))
    assert event_paths(gateway)[-1] == f"{EVENTS}?type=refunds.%2A&prev_item=evt-1&max_items=1"


def test_paging_that_comes_back_to_a_fetched_page_stops_after_yielding_it(
    client: GoPayClient, gateway: FakeGateway, errors: ErrorLog
) -> None:
    gateway.on(
        "GET", f"{EVENTS}?type=payments.paid&max_items=1", page(ev(1), next_query="prev_item=a")
    )
    # The same query as the first page, its parameters in another order.
    gateway.on(
        "GET", f"{EVENTS}?prev_item=a", page(ev(2), next_query="max_items=1&type=payments.paid")
    )
    seen: list[str] = []
    events = client.iter_events("8123456789", {"type": "payments.paid", "max_items": 1})
    with pytest.raises(GoPaySDKError) as caught:
        collect(events, seen)
    assert seen == ["evt-1", "evt-2"]
    assert caught.value.error_code is ErrorCode.UNEXPECTED_RESPONSE
    assert "already fetched" in str(caught.value)
    assert caught.value.trace_id == gateway.last().headers["x-gopay-trace-id"]
    assert errors.errors == [caught.value]
    assert len(gateway.api_requests()) == 2


def test_a_cursor_that_repeats_itself_stops_the_paging(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", EVENTS, page(ev(1), next_query="prev_item=a"))
    gateway.on("GET", f"{EVENTS}?prev_item=a", page(ev(2), next_query="prev_item=a"))
    with pytest.raises(GoPaySDKError, match="already fetched"):
        list(client.iter_events("8123456789"))
    assert len(gateway.api_requests()) == 2


def test_an_empty_page_naming_a_next_one_stops_the_paging(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", EVENTS, page(ev(1), next_query="prev_item=a"))
    gateway.on("GET", f"{EVENTS}?prev_item=a", page(next_query="prev_item=b"))
    seen: list[str] = []
    with pytest.raises(GoPaySDKError, match="empty page") as caught:
        collect(client.iter_events("8123456789"), seen)
    assert seen == ["evt-1"]
    assert caught.value.error_code is ErrorCode.UNEXPECTED_RESPONSE
    assert len(gateway.api_requests()) == 2


def test_a_page_without_items_mid_iteration_is_reported_once(
    client: GoPayClient, gateway: FakeGateway, errors: ErrorLog
) -> None:
    gateway.on("GET", EVENTS, page(ev(1), next_query="prev_item=a"))
    gateway.on("GET", f"{EVENTS}?prev_item=a", reply(200, {"next_page_url": None}))
    events = client.iter_events("8123456789")
    assert next(events)["id"] == "evt-1"
    with pytest.raises(GoPaySDKError) as caught:
        next(events)
    assert caught.value.error_code is ErrorCode.UNEXPECTED_RESPONSE
    assert errors.errors == [caught.value]


def test_a_failed_page_request_is_reported_once_with_its_ids(
    client: GoPayClient, gateway: FakeGateway, errors: ErrorLog
) -> None:
    gateway.on("GET", EVENTS, page(ev(1), next_query="prev_item=a"))
    gateway.on("GET", f"{EVENTS}?prev_item=a", reply(400, {"errors": [{"error_code": 400}]}))
    with pytest.raises(GoPayHTTPError) as caught:
        list(client.iter_events("8123456789"))
    assert caught.value.trace_id == gateway.last().headers["x-gopay-trace-id"]
    assert errors.errors == [caught.value]


def test_every_page_goes_out_under_the_transaction_of_the_call(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", EVENTS, page(ev(1), next_query="prev_item=a"))
    gateway.on("GET", f"{EVENTS}?prev_item=a", page(ev(2), next_query="prev_item=b"))
    gateway.on("GET", f"{EVENTS}?prev_item=b", page(ev(3)))
    with client.with_transaction_id("sync-run-1"):
        events = client.iter_events("8123456789")
    # Iterated outside the block, and partly inside another one: still the call's id.
    first = next(events)
    with client.with_transaction_id("something-else"):
        rest = list(events)
    assert [first["id"], *(e["id"] for e in rest)] == ["evt-1", "evt-2", "evt-3"]
    assert {r.headers["x-gopay-transaction-id"] for r in gateway.api_requests()} == {"sync-run-1"}
    assert len({r.headers["x-gopay-trace-id"] for r in gateway.api_requests()}) == 3


def test_iterations_of_separate_calls_have_separate_transactions(
    client: GoPayClient, gateway: FakeGateway
) -> None:
    gateway.on("GET", EVENTS, page(ev(1)))
    list(client.iter_events("8123456789"))
    list(client.iter_events("8123456789"))
    first, second = gateway.api_requests()
    assert first.headers["x-gopay-transaction-id"] != second.headers["x-gopay-transaction-id"]


@pytest.mark.parametrize(
    ("goid", "params"),
    [
        pytest.param("..", None, id="goid"),
        pytest.param("8123456789", {"occurred_after": datetime.datetime(2026, 7, 1)}, id="naive"),
    ],
)
def test_iter_events_checks_its_arguments_at_the_call_not_at_the_first_page(
    client: GoPayClient, gateway: FakeGateway, errors: ErrorLog, goid: str, params: Any
) -> None:
    with pytest.raises(GoPaySDKError) as caught:
        client.iter_events(goid, params)
    assert caught.value.error_code is ErrorCode.INVALID_ARGUMENT
    assert errors.errors == [caught.value]
    assert gateway.api_requests() == []


def test_close_stops_the_paging(client: GoPayClient, gateway: FakeGateway) -> None:
    gateway.on("GET", EVENTS, page(ev(1), ev(2), next_query="prev_item=b"))
    events = client.iter_events("8123456789")
    next(events)
    events.close()  # type: ignore[attr-defined]
    assert list(events) == []
    assert len(gateway.api_requests()) == 1


def test_the_event_and_shared_scopes(
    make_client: Callable[..., GoPayClient], gateway: FakeGateway
) -> None:
    assert GoPayScopes.EVENT_READ == "event:read"
    assert GoPayScopes.SHARED_READ == "shared:read"
    client = make_client()
    client.authenticate(
        "id", "secret", combine_scopes(GoPayScopes.EVENT_READ, GoPayScopes.SHARED_READ)
    )
    assert "event%3Aread+shared%3Aread" in gateway.token_requests()[0].content.decode()
