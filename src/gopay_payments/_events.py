"""Events of an eshop, as webhooks deliver them and the Events API lists them."""

from __future__ import annotations

from typing import Any, Union, get_args

from typing_extensions import TypedDict, TypeGuard

from ._generated import Event, EventType

__all__ = ["KNOWN_EVENT_TYPES", "GoPayEvent", "UnknownEvent", "is_event", "is_known_event"]


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
