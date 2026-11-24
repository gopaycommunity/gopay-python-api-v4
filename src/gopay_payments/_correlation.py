"""Request ids GoPay's API v4 logs, so a call can be found from either side.

- ``X-GoPay-Trace-Id``: one per HTTP request. A retry resends the same request and keeps it.
- ``X-GoPay-Transaction-Id``: one per public SDK call, shared by every request the call makes —
  the token refresh, the retries, the polls of an ``await_*`` method — or the merchant's own,
  set with :meth:`GoPayClient.with_transaction_id`.
- ``X-GoPay-Payment-Session-Id``: the payment id, on requests under ``/payments/{id}``.

The transaction id lives in a :class:`contextvars.ContextVar`, not on the client: one client
is shared between threads, and each thread — each asyncio task too — needs an id of its own.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Optional

from .errors import ErrorCode, GoPayError, GoPaySDKError

TRACE_ID_HEADER = "X-GoPay-Trace-Id"
TRANSACTION_ID_HEADER = "X-GoPay-Transaction-Id"
PAYMENT_SESSION_ID_HEADER = "X-GoPay-Payment-Session-Id"

# What the gateway accepts as a transaction id; anything else would be dropped from its log.
_TRANSACTION_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
# Still percent-encoded as it went into the path: a header value must stay ASCII.
_PAYMENT_PATH = re.compile(r"^/?payments/([^/?#]+)")

# Module level, as a ContextVar has to be: a context keeps every variable it has seen alive,
# so one created per client would never be freed.
_transaction_id: ContextVar[Optional[str]] = ContextVar(
    "gopay_payments_transaction_id", default=None
)


@dataclass(frozen=True)
class RequestIds:
    """The ids one HTTP request goes out with."""

    trace_id: str
    transaction_id: str


def new_id() -> str:
    return str(uuid.uuid4())


def require_transaction_id(value: Any) -> str:
    if not isinstance(value, str) or not _TRANSACTION_ID.fullmatch(value):
        raise GoPaySDKError(
            "[GoPaySDK] transaction_id must be 1 to 64 characters of "
            'A-Z, a-z, 0-9, ".", "_" or "-"',
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
    return value


def current_transaction_id() -> Optional[str]:
    return _transaction_id.get()


@contextmanager
def transaction(transaction_id: Optional[str] = None) -> Iterator[None]:
    """Run the block as one transaction.

    Under ``transaction_id`` when given; otherwise under the transaction already running (an SDK
    method called from another, or inside :meth:`GoPayClient.with_transaction_id`), else under
    a new one.
    """
    if transaction_id is None and _transaction_id.get() is not None:
        yield
        return
    token = _transaction_id.set(transaction_id if transaction_id is not None else new_id())
    try:
        yield
    finally:
        _transaction_id.reset(token)


def request_ids() -> RequestIds:
    """A new trace id under the running transaction — or a new one outside any SDK call."""
    return RequestIds(trace_id=new_id(), transaction_id=_transaction_id.get() or new_id())


def headers_for(ids: RequestIds, path: str) -> dict[str, str]:
    headers = {TRACE_ID_HEADER: ids.trace_id, TRANSACTION_ID_HEADER: ids.transaction_id}
    match = _PAYMENT_PATH.match(path)
    if match:
        headers[PAYMENT_SESSION_ID_HEADER] = match.group(1)
    return headers


def tag(error: GoPayError, ids: RequestIds) -> GoPayError:
    """Record on ``error`` the request it came from, unless it already names one.

    An error raised by an inner request — the token refresh — keeps that request's ids.
    """
    if error.trace_id is None:
        error.trace_id = ids.trace_id
        error.transaction_id = ids.transaction_id
    return error
