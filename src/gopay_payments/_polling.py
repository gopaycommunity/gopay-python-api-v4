from __future__ import annotations

import threading
import time
from collections.abc import Collection, Mapping
from typing import Callable, Optional, TypeVar

from .errors import ErrorCode, GoPaySDKError

T = TypeVar("T", bound=Mapping[str, object])

# Indirection so tests can drive the loops without real time passing.
_clock: Callable[[], float] = time.monotonic


def _pause(seconds: float, cancel: Optional[threading.Event]) -> bool:
    """Sleep for ``seconds``; return True if ``cancel`` was set meanwhile."""
    seconds = max(0.0, seconds)
    if cancel is not None:
        return cancel.wait(seconds)
    time.sleep(seconds)
    return False


class _Deadline:
    """A point in time polling must not run past; ``None`` seconds means no limit."""

    def __init__(self, seconds: Optional[float]) -> None:
        self._at = None if seconds is None else _clock() + seconds

    def lift(self) -> None:
        self._at = None

    def expired(self) -> bool:
        return self._at is not None and _clock() >= self._at

    def remaining(self, interval: float) -> float:
        """How long to pause: the interval, cut short so the deadline is not overslept."""
        return interval if self._at is None else min(interval, self._at - _clock())


class _RedirectNotifier:
    """Hands each distinct 3-D Secure redirect URL to the callback, once."""

    def __init__(self, callback: Optional[Callable[[str], object]]) -> None:
        self._callback = callback
        self._last: Optional[str] = None

    def notify(self, state: Mapping[str, object]) -> None:
        action = state.get("action")
        redirect = action.get("redirect_url") if isinstance(action, Mapping) else None
        if not isinstance(redirect, str) or not redirect or redirect == self._last:
            return
        self._last = redirect
        if self._callback is not None:
            self._callback(redirect)


def _poll_once(poll: Callable[[], T], on_state_change: Optional[Callable[[T], object]]) -> T:
    state = poll()
    if on_state_change is not None:
        on_state_change(state)
    return state


def _wait(
    deadline: _Deadline,
    interval: float,
    cancel: Optional[threading.Event],
    *,
    what: str,
    timeout_error: Callable[[], GoPaySDKError],
) -> None:
    """Pause before the next poll, raising on a deadline passed or a cancel requested."""
    if deadline.expired():
        raise timeout_error()
    if _pause(deadline.remaining(interval), cancel):
        raise _aborted(what)
    if deadline.expired():
        raise timeout_error()


def await_charge(
    poll: Callable[[], T],
    *,
    interval: float,
    initial_timeout: float,
    on_state_change: Optional[Callable[[T], object]],
    on_action_required: Optional[Callable[[str], object]],
    cancel: Optional[threading.Event],
) -> T:
    """Poll until the charge settles.

    Returns on ``SUCCEEDED``. Raises ``CHARGE_FAILED`` on ``FAILED`` (with the state on
    ``charge_state``) and ``CHARGE_TIMEOUT`` when the charge has not left
    ``REQUESTED``/``PROCESSING`` within ``initial_timeout``. The timeout stops applying
    once ``ACTION_REQUIRED`` is seen: the customer may take as long as they need with 3-D
    Secure. ``on_action_required`` fires once per distinct redirect URL.
    """
    _raise_if_cancelled(cancel, "Charge")
    deadline = _Deadline(initial_timeout)
    redirects = _RedirectNotifier(on_action_required)
    while True:
        state = _poll_once(poll, on_state_change)
        current = state.get("state")
        if current == "SUCCEEDED":
            return state
        if current == "FAILED":
            raise GoPaySDKError(
                "[GoPaySDK] Charge failed", error_code=ErrorCode.CHARGE_FAILED, charge_state=state
            )
        if current == "ACTION_REQUIRED":
            deadline.lift()
            redirects.notify(state)
        _wait(deadline, interval, cancel, what="Charge", timeout_error=_charge_timeout)


def await_state(
    poll: Callable[[], T],
    *,
    what: str,
    terminal_states: Collection[str],
    interval: float,
    timeout: Optional[float],
    on_state_change: Optional[Callable[[T], object]],
    cancel: Optional[threading.Event],
) -> T:
    """Poll until ``state`` is one of ``terminal_states`` and return that response.

    There is no timeout unless one is given: a QR or bank-transfer payment, a refund or a
    recurrence may stay pending far longer than any sensible client-side ceiling, and the
    API ends it on its own schedule. Raises ``CHARGE_TIMEOUT`` when ``timeout`` elapses.
    """
    _raise_if_cancelled(cancel, what)
    terminal = frozenset(terminal_states)
    deadline = _Deadline(timeout)
    while True:
        state = _poll_once(poll, on_state_change)
        if state.get("state") in terminal:
            return state
        _wait(deadline, interval, cancel, what=what, timeout_error=lambda: _state_timeout(what))


def _raise_if_cancelled(cancel: Optional[threading.Event], what: str) -> None:
    if cancel is not None and cancel.is_set():
        raise _aborted(what)


def _aborted(what: str) -> GoPaySDKError:
    return GoPaySDKError(
        f"[GoPaySDK] {what} polling cancelled.", error_code=ErrorCode.CHARGE_FAILED
    )


def _charge_timeout() -> GoPaySDKError:
    return GoPaySDKError(
        "[GoPaySDK] Charge did not progress within the initial timeout",
        error_code=ErrorCode.CHARGE_TIMEOUT,
    )


def _state_timeout(what: str) -> GoPaySDKError:
    return GoPaySDKError(
        f"[GoPaySDK] {what} did not reach a terminal state within the timeout.",
        error_code=ErrorCode.CHARGE_TIMEOUT,
    )
