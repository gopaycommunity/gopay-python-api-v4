"""Events of an eshop, as webhooks deliver them and the Events API lists them."""

from __future__ import annotations

import datetime
from typing import Any, Optional, Union, cast, get_args
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from typing_extensions import Literal, NotRequired, TypedDict, TypeGuard

from ._generated import Event, EventType
from .errors import ErrorCode, GoPaySDKError

__all__ = [
    "KNOWN_EVENT_TYPES",
    "EventPage",
    "GoPayEvent",
    "ListEventsParams",
    "UnknownEvent",
    "is_event",
    "is_known_event",
]


class UnknownEvent(TypedDict):
    """An event whose ``type`` this SDK version does not know — one GoPay added after the release.

    It arrives as sent, never as an error: the API asks for a type the merchant does not
    recognise to be ignored, so a new type must not break a working integration. Upgrading
    the SDK gives it a model of its own.
    """

    id: str
    type: str
    occurred_at: str
    data: dict[str, Any]


GoPayEvent = Union[Event, UnknownEvent]
"""An event of the eshop. Check :func:`is_known_event` before relying on ``data``."""

# From the generated enum, so the next codegen adds a new type here by itself.
KNOWN_EVENT_TYPES: frozenset[str] = frozenset(get_args(EventType))


def is_known_event(event: GoPayEvent) -> TypeGuard[Event]:
    """Whether ``event`` is one of the types this SDK version knows, with its ``data`` modelled.

    >>> if is_known_event(event) and event["type"] == "payments.paid":
    ...     mark_paid(event["data"]["payment_id"])
    """
    return event.get("type") in KNOWN_EVENT_TYPES


def is_event(value: Any) -> TypeGuard[GoPayEvent]:
    """Whether ``value`` has the fields every event has.

    Checked so that a known ``type`` can be trusted to carry its ``data``. The fields of
    ``data`` itself are not: a known event keeps them as sent.
    """
    return (
        isinstance(value, dict)
        and isinstance(value.get("id"), str)
        and isinstance(value.get("type"), str)
        and isinstance(value.get("occurred_at"), str)
        and isinstance(value.get("data"), dict)
    )


class EventPage(TypedDict):
    """One page of ``GET /eshops/{goid}/events``, newest event first.

    ``next_page_url`` is set while older matching events remain. Follow it with nothing of
    your own: :meth:`GoPayClient.iter_events` pages for you.
    """

    items: list[GoPayEvent]
    next_page_url: NotRequired[str]


class ListEventsParams(TypedDict, total=False):
    """Filters and paging of ``GET /eshops/{goid}/events``.

    ``occurred_after`` and ``occurred_before`` take an aware :class:`datetime.datetime` (sent
    with its milliseconds, which the API orders events by) or an RFC 3339 string.
    """

    max_items: int
    """Events in one page, 1 to 100 (the API's default is 20)."""
    prev_item: str
    """The ``id`` of the last event of the previous page. GoPay writes it into
    ``next_page_url``; you do not need to set it."""
    type: str
    """Comma-separated types or namespaces: ``"refunds.succeeded,payments.*"``."""
    occurred_after: Union[datetime.datetime, str]
    occurred_before: Union[datetime.datetime, str]
    related_type: Literal["payments", "links", "refunds", "recurrences", "cards"]
    """The kind of entity ``related_id`` refers to. Required together with it."""
    related_id: str


def events_query(params: Optional[ListEventsParams]) -> str:
    """The query string of ``params``, RFC 3986 encoded so ``+`` in an offset is ``%2B``."""
    if params is None:
        return ""
    if not isinstance(params, dict):
        raise GoPaySDKError(
            "[GoPaySDK] params must be a dict of event filters",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
    pairs: list[tuple[str, str]] = []
    for name, value in params.items():
        if value is None:
            continue
        if isinstance(value, datetime.datetime):
            if value.utcoffset() is None:
                raise GoPaySDKError(
                    f"[GoPaySDK] {name} must be a timezone-aware datetime, "
                    "e.g. datetime.now(timezone.utc)",
                    error_code=ErrorCode.INVALID_ARGUMENT,
                )
            # Milliseconds kept: an instant cut to the second would move an
            # occurred_after bound back and return events the caller has already seen.
            pairs.append((name, value.isoformat(timespec="milliseconds")))
        elif isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool)):
            pairs.append((name, str(value)))
        else:
            raise GoPaySDKError(
                f"[GoPaySDK] {name} must be a string, an integer or a datetime",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
    return urlencode(pairs, quote_via=quote)


def next_page_query(next_page_url: Any) -> Optional[str]:
    """The query of ``next_page_url`` as the gateway wrote it, less the empty pairs a stray ``&``
    leaves. ``None`` when nothing usable is left.

    Only the query is used, sent to the base URL the client was configured with: following
    the URL itself would hand the access token to whatever host it names.
    """
    if not isinstance(next_page_url, str):
        return None
    pairs = [
        pair
        for pair in urlsplit(next_page_url).query.split("&")
        if pair and not pair.startswith("=")
    ]
    return "&".join(pairs) or None


def page_key(query: str) -> tuple[tuple[str, str], ...]:
    """A query in one order, so two cursors naming one page compare equal."""
    return tuple(sorted(parse_qsl(query, keep_blank_values=True)))


def unexpected_events() -> GoPaySDKError:
    return GoPaySDKError(
        "[GoPaySDK] Failed to read the events response: it is not a list of events.",
        error_code=ErrorCode.UNEXPECTED_RESPONSE,
    )


def read_page(data: Any) -> EventPage:
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not all(is_event(item) for item in items):
        raise unexpected_events()
    following = data.get("next_page_url")
    if following in (None, ""):
        # Absent, as the type says, when there is no next page — not null or empty.
        data.pop("next_page_url", None)
    elif next_page_query(following) is None:
        raise unexpected_events()
    return cast(EventPage, data)


def read_event(data: Any) -> GoPayEvent:
    if not is_event(data):
        raise GoPaySDKError(
            "[GoPaySDK] Failed to read the event response: it is not an event.",
            error_code=ErrorCode.UNEXPECTED_RESPONSE,
        )
    return data


def paging_stuck(reason: str) -> GoPaySDKError:
    return GoPaySDKError(
        f"[GoPaySDK] Event paging stopped: {reason}, which would page forever.",
        error_code=ErrorCode.UNEXPECTED_RESPONSE,
    )
