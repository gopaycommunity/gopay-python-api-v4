"""Errors raised by the SDK.

Two kinds, with a common base so a caller can catch either:

- :class:`GoPayHTTPError` — the API answered with a non-2xx status. ``status`` and
  ``body`` carry what it said.
- :class:`GoPaySDKError` — the SDK itself gave up: bad arguments, no credentials, a
  timeout, a charge that failed while polling. ``error_code`` says which.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional


class ErrorCode(str, Enum):
    """Machine-readable reason carried by :class:`GoPaySDKError`.

    The values are the strings the PHP SDK uses, and the JavaScript SDK the ones that apply to
    a server. ``UNEXPECTED_RESPONSE`` exists only in PHP and here.
    """

    # An error code, not a secret: the S105 check takes any *TOKEN* name for one.
    AUTH_TOKEN_MISSING = "AUTH_TOKEN_MISSING"  # noqa: S105
    """No access token is available — call ``authenticate()`` first."""
    AUTH_REFRESH_FAILED = "AUTH_REFRESH_FAILED"
    """Obtaining a new access token with the stored credentials failed."""
    AUTH_INVALID_RESPONSE = "AUTH_INVALID_RESPONSE"
    """The token endpoint answered with something that is not a token: not JSON, or without
    the fields a token needs."""
    AUTH_CREDENTIALS_MISSING = "AUTH_CREDENTIALS_MISSING"
    """The operation needs credentials or a shareable key the client does not have."""
    AUTH_UNAUTHORIZED = "AUTH_UNAUTHORIZED"
    """The API rejected a freshly obtained token — usually a missing OAuth2 scope."""
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    """The request did not complete within the configured timeout."""
    NETWORK_ERROR = "NETWORK_ERROR"
    """No response arrived: the connection failed or dropped."""
    UNEXPECTED_RESPONSE = "UNEXPECTED_RESPONSE"
    """The API answered 2xx with a body the SDK cannot use — empty, not JSON, or not the
    JSON object (or list) the operation returns."""
    CHARGE_TIMEOUT = "CHARGE_TIMEOUT"
    """Polling gave up before the charge, payment, refund or recurrence settled."""
    CHARGE_FAILED = "CHARGE_FAILED"
    """The charge reached ``FAILED``, or polling was cancelled."""
    WEBHOOK_INVALID = "WEBHOOK_INVALID"
    """The request is not a webhook GoPay signed: answer it with a 4xx. Never passed to
    ``on_error`` — anyone who can reach the endpoint can cause one."""
    WEBHOOK_KEYS_UNAVAILABLE = "WEBHOOK_KEYS_UNAVAILABLE"
    """The webhook could not be checked because the signing keys could not be loaded: answer
    it with a 5xx, so GoPay sends it again. Not passed to ``on_error``; the failed key request
    is, once."""
    INVALID_CONFIG = "INVALID_CONFIG"
    """The client configuration is invalid."""
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    """A required argument is missing or invalid."""


class GoPayError(Exception):
    """Base class of every error the SDK raises.

    ``trace_id`` and ``transaction_id`` are the ids GoPay's log records for the request the
    error came from: quote them to GoPay support and the request can be found.
    """

    # Set once the error has been handed to ``on_error``, so an error crossing several
    # layers on its way out is reported exactly once.
    _reported: bool = False

    trace_id: Optional[str] = None
    """``X-GoPay-Trace-Id`` of the HTTP request the error came from — also on a timeout or a
    network error, where no response arrived. ``None`` when no request went out (an invalid
    argument, no access token). Set by the SDK."""

    transaction_id: Optional[str] = None
    """``X-GoPay-Transaction-Id`` of the SDK call the error came from, shared by every request
    the call made (token refresh, retries, polling). ``None`` when ``trace_id`` is."""


class GoPaySDKError(GoPayError):
    """The SDK could not complete the operation.

    ``charge_state`` holds the final charge state when ``error_code`` is
    ``CHARGE_FAILED`` because the charge reached ``FAILED`` — ``fail_reason`` in it says
    why, without needing an ``on_state_change`` callback.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: Optional[ErrorCode] = None,
        charge_state: Any = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.charge_state = charge_state


class GoPayHTTPError(GoPayError):
    """The API answered with a non-2xx status.

    ``body`` is the parsed JSON response, or the raw text when it was not JSON.
    ``endpoint`` is the request path with ids collapsed to ``{id}``
    (``/payments/{id}/charge``) — a stable key for grouping errors in monitoring.
    ``retry_after`` is the wait in seconds the response asked for in ``Retry-After``, or
    ``None`` when it named none. A read is retried on its own after a ``429`` asking for 10
    seconds or less; a longer wait, and every write, comes back to the caller with it.
    """

    def __init__(
        self,
        status: int,
        body: Any,
        *,
        method: Optional[str] = None,
        endpoint: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        # The message stays free of the endpoint and the body: callers match on it, and
        # the details belong in fields a tracker can read.
        super().__init__(f"GoPay API error: HTTP {status}")
        self.status = status
        self.body = body
        self.method = method
        self.endpoint = endpoint
        self.retry_after = retry_after

    def __reduce__(self) -> tuple[Any, ...]:
        # The default rebuilds an exception from its message alone, which this constructor
        # does not take: without this, pickle and copy fail, and so does handing the error
        # across a process boundary (Celery, ProcessPoolExecutor).
        return (type(self), (self.status, self.body), self.__dict__)
