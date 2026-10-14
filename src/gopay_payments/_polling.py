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


def _aborted(what: str) -> GoPaySDKError:
    return GoPaySDKError(
        f"[GoPaySDK] {what} polling cancelled.", error_code=ErrorCode.CHARGE_FAILED
    )


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
    if cancel is not None and cancel.is_set():
        raise _aborted("Charge")
    deadline: Optional[float] = _clock() + initial_timeout
    last_redirect: Optional[str] = None
    while True:
        state = poll()
        if on_state_change is not None:
            on_state_change(state)
        current = state.get("state")
        if current == "SUCCEEDED":
            return state
        if current == "FAILED":
            raise GoPaySDKError(
                "[GoPaySDK] Charge failed", error_code=ErrorCode.CHARGE_FAILED, charge_state=state
            )
        if current == "ACTION_REQUIRED":
            deadline = None
            action = state.get("action")
            redirect = action.get("redirect_url") if isinstance(action, Mapping) else None
            if isinstance(redirect, str) and redirect and redirect != last_redirect:
                last_redirect = redirect
                if on_action_required is not None:
                    on_action_required(redirect)
        if _timed_out(deadline):
            raise _charge_timeout()
        wait = interval if deadline is None else min(interval, deadline - _clock())
        if _pause(wait, cancel):
            raise _aborted("Charge")
        if _timed_out(deadline):
            raise _charge_timeout()


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
    if cancel is not None and cancel.is_set():
        raise _aborted(what)
    terminal = frozenset(terminal_states)
    deadline = None if timeout is None else _clock() + timeout
    while True:
        state = poll()
        if on_state_change is not None:
            on_state_change(state)
        if state.get("state") in terminal:
            return state
        if _timed_out(deadline):
            raise _state_timeout(what)
        wait = interval if deadline is None else min(interval, deadline - _clock())
        if _pause(wait, cancel):
            raise _aborted(what)
        if _timed_out(deadline):
            raise _state_timeout(what)


def _timed_out(deadline: Optional[float]) -> bool:
    return deadline is not None and _clock() >= deadline


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
