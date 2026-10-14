"""The GoPay Payments API v4 client."""

from __future__ import annotations

import functools
import math
import threading
from collections.abc import Collection
from typing import Any, Callable, Optional, TypeVar, Union, cast

import httpx
from typing_extensions import Concatenate, Literal, ParamSpec, Self, TypedDict

from . import _polling
from ._generated import (
    LinkCreateRequest,
    LinkDetails,
    PaymentChargeInput,
    PaymentChargeResponse,
    PaymentChargeStatusResponse,
    PaymentCreateRequest,
    PaymentDetails,
    PaymentInstanceOverride,
    PermanentCardTokenDetails,
    QRPaymentDetails,
    RecurrenceCreateRequest,
    RecurrenceDetails,
    RefundCreateRequest,
    RefundDetails,
    TokenCreateRequest,
    ValidateMerchantRequest,
)
from ._transport import OnError, Transport, report
from ._validate import (
    require_header_value,
    require_https_origin,
    require_non_empty_string,
    require_path_segment,
)
from ._version import USER_AGENT
from .environment import Environment, parse_environment
from .errors import ErrorCode, GoPayError, GoPaySDKError
from .token_cache import TokenCache

__all__ = ["BrowserKeys", "GoPayClient"]

P = ParamSpec("P")
R = TypeVar("R")

PAYMENT_TERMINAL_STATES = ("PAID", "CANCELED", "TIMEOUTED", "REFUNDED", "PARTIALLY_REFUNDED")
REFUND_TERMINAL_STATES = ("SUCCESS", "FAILED")
RECURRENCE_TERMINAL_STATES = ("STARTED", "STOPPED")
# The API asks to be polled about once a second. A lower interval would only load it.
MIN_POLL_INTERVAL = 1.0
# States of the first payment from which a REQUESTED recurrence can never reach STARTED.
UNPAYABLE_PAYMENT_STATES = frozenset({"CANCELED", "TIMEOUTED"})


class BrowserKeys(TypedDict):
    """What the browser SDK needs to start, returned by :meth:`GoPayClient.get_browser_keys`."""

    shareable_key: str
    client_id: str


def _reported(
    method: Callable[Concatenate[GoPayClient, P], R],
) -> Callable[Concatenate[GoPayClient, P], R]:
    """Report every SDK error a public method raises to ``on_error``.

    Errors raised during a request would reach ``on_error`` anyway, but argument
    validation and polling outcomes happen outside any request. Wrapping the method
    covers them by construction, and the once-per-error guard keeps an error that crossed
    several layers from being reported twice.
    """

    @functools.wraps(method)
    def wrapper(self: GoPayClient, /, *args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return method(self, *args, **kwargs)
        except GoPayError as error:
            self._transport.report(error)
            raise

    return wrapper


class GoPayClient:
    """Server-side client for the GoPay Payments API v4.

    Create one per eshop configuration and reuse it: it holds the access token and a
    connection pool, and is safe to share between threads.

    >>> client = GoPayClient(environment="sandbox")
    >>> client.authenticate(client_id, client_secret, combine_scopes(
    ...     GoPayScopes.PAYMENT_WRITE, GoPayScopes.PAYMENT_READ))
    >>> payment = client.create_payment(goid, {...})

    :param environment: ``"sandbox"`` (default) or ``"production"``.
    :param base_url: Override the API base URL — a mock server in tests, for example.
        Must be HTTPS; plain HTTP is accepted only for localhost in the sandbox.
    :param timeout: Per-request timeout in seconds.
    :param user_agent_suffix: Appended to the SDK's own ``User-Agent`` (``"MyShop/2.1"``),
        never replacing it.
    :param shareable_key: The eshop's shareable key, returned by :meth:`get_browser_keys`.
    :param on_error: Called once for every error the SDK raises, before it propagates.
        It observes and cannot suppress; anything it raises is logged and ignored, so
        monitoring being down never replaces the SDK's own error.
    :param token_cache: Where to keep the access token. Defaults to the client's memory;
        pass a shared cache when many processes use the same credentials.
    :param http_client: An ``httpx.Client`` to send requests through — proxies, custom
        TLS, test transports. The SDK does not close a client it did not create.
    """

    def __init__(
        self,
        *,
        environment: Union[Environment, Literal["sandbox", "production"]] = Environment.SANDBOX,
        base_url: Optional[str] = None,
        timeout: float = 10.0,
        user_agent_suffix: Optional[str] = None,
        shareable_key: Optional[str] = None,
        on_error: Optional[OnError] = None,
        token_cache: Optional[TokenCache] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        try:
            env = parse_environment(environment)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
                raise GoPaySDKError(
                    "[GoPaySDK] timeout must be a positive number of seconds",
                    error_code=ErrorCode.INVALID_CONFIG,
                )
            user_agent = USER_AGENT
            if user_agent_suffix is not None:
                user_agent += " " + require_header_value(user_agent_suffix, "user_agent_suffix")
            self._shareable_key = (
                None
                if shareable_key is None
                else require_non_empty_string(shareable_key, "shareable_key")
            )
            self._transport = Transport(
                environment=env,
                base_url=base_url,
                timeout=float(timeout),
                user_agent=user_agent,
                http_client=http_client,
                token_cache=token_cache,
                on_error=on_error,
            )
        except GoPayError as error:
            # Construction errors happen before any method exists to report them, and are
            # the one kind guaranteed to happen at start-up.
            report(on_error, error)
            raise

    # ----------------------------------------------------------------- lifecycle

    @property
    def environment(self) -> Environment:
        return self._transport.environment

    @property
    def user_agent(self) -> str:
        """The ``User-Agent`` header this client sends."""
        return self._transport.user_agent

    def close(self) -> None:
        """Release the connection pool (only if this client created it)."""
        self._transport.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---------------------------------------------------------------------- auth

    @_reported
    def authenticate(self, client_id: str, client_secret: str, scope: str) -> None:
        """Authenticate with the eshop's client credentials.

        The access token stays inside the client and is never returned: it must not leave
        the server. Expired tokens are replaced on their own, so this is needed once, not
        before every call. When the token cache already holds a valid token for these
        credentials and scope, no request is made.

        :param scope: Space-separated OAuth2 scopes — see :class:`GoPayScopes`.
        """
        self._transport.authenticate(
            require_non_empty_string(client_id, "client_id"),
            require_non_empty_string(client_secret, "client_secret"),
            require_non_empty_string(scope, "scope"),
        )

    def is_authenticated(self) -> bool:
        """Whether a token is held. Does not check expiry — that is handled per request."""
        return self._transport.has_token()

    def logout(self) -> None:
        """Forget the credentials and drop the token from the cache."""
        self._transport.logout()

    @_reported
    def set_shareable_key(self, key: str) -> None:
        """Set the shareable key returned by :meth:`get_browser_keys`."""
        self._shareable_key = require_non_empty_string(key, "shareable_key")

    @_reported
    def get_browser_keys(self) -> BrowserKeys:
        """Return the public keys the browser SDK is initialised with.

        Both are safe to send to the browser. Needs ``shareable_key`` and a prior
        :meth:`authenticate`, which supplies the client id.
        """
        client_id = self._transport.client_id
        if not self._shareable_key or not client_id:
            raise GoPaySDKError(
                "[GoPaySDK] get_browser_keys() requires shareable_key "
                "and a prior authenticate() call.",
                error_code=ErrorCode.AUTH_CREDENTIALS_MISSING,
            )
        return {"shareable_key": self._shareable_key, "client_id": client_id}

    # ------------------------------------------------------------------ payments

    @_reported
    def create_payment(self, goid: Union[str, int], params: PaymentCreateRequest) -> PaymentDetails:
        """Create a payment. ``POST /eshops/{goid}/payments``

        The response carries ``gw_url``, the address of GoPay's hosted payment gateway,
        which offers the customer every method the eshop has enabled. It is a supported
        integration of its own, not a legacy endpoint — but drive either the gateway or
        :meth:`charge_payment` for a given payment, never both. QR and bank-transfer
        payments are not charged: show the customer the payment details and follow the
        payment with :meth:`await_payment_status`.
        """
        path = f"/eshops/{_goid(goid)}/payments"
        return cast(PaymentDetails, self._transport.request("POST", path, body=params))

    @_reported
    def get_payment_status(self, payment_id: str) -> PaymentDetails:
        """Return the current state of a payment. ``GET /payments/{payment_id}``"""
        pid = require_path_segment(payment_id, "payment_id")
        return cast(PaymentDetails, self._transport.request("GET", f"/payments/{pid}"))

    @_reported
    def cancel_payment(self, payment_id: str) -> None:
        """Cancel a payment that has not been charged. ``DELETE /payments/{payment_id}``

        Only a payment in ``CREATED`` can be canceled; any other state answers ``409``
        and leaves the payment unchanged. It is the state that decides, not whether a
        charge was attempted. The payment moves to ``CANCELED`` and stays readable.
        """
        pid = require_path_segment(payment_id, "payment_id")
        self._transport.request("DELETE", f"/payments/{pid}")

    @_reported
    def charge_payment(self, payment_id: str, params: PaymentChargeInput) -> PaymentChargeResponse:
        """Charge a payment. ``POST /payments/{payment_id}/charge``

        The response is not the outcome: the charge is processed asynchronously. Follow it
        with :meth:`await_charge_state`, which hands you the 3-D Secure redirect when the
        bank asks for one.

        A card charge needs ``browser_data`` whose ``ip``, ``user_agent`` and
        ``accept_header`` come from the customer's browser — fetch them there with the
        browser SDK and pass them through unchanged. Values taken from the request this
        server received look valid and then fail 3-D Secure.
        """
        pid = require_path_segment(payment_id, "payment_id")
        path = f"/payments/{pid}/charge"
        return cast(PaymentChargeResponse, self._transport.request("POST", path, body=params))

    @_reported
    def get_charge_state(self, payment_id: str) -> PaymentChargeStatusResponse:
        """Return the current state of a charge. ``GET /payments/{payment_id}/charge``"""
        pid = require_path_segment(payment_id, "payment_id")
        path = f"/payments/{pid}/charge"
        return cast(PaymentChargeStatusResponse, self._transport.request("GET", path))

    @_reported
    def get_google_pay_info(self, payment_id: str) -> dict[str, Any]:
        """Return the Google Pay configuration for a payment.
        ``GET /payments/{payment_id}/google-pay/info``"""
        pid = require_path_segment(payment_id, "payment_id")
        path = f"/payments/{pid}/google-pay/info"
        return cast("dict[str, Any]", self._transport.request("GET", path))

    @_reported
    def get_apple_pay_info(self, payment_id: str) -> dict[str, Any]:
        """Return the Apple Pay configuration for a payment.
        ``GET /payments/{payment_id}/apple-pay/info``"""
        pid = require_path_segment(payment_id, "payment_id")
        path = f"/payments/{pid}/apple-pay/info"
        return cast("dict[str, Any]", self._transport.request("GET", path))

    @_reported
    def validate_apple_pay_merchant(
        self,
        payment_id: str,
        params: Optional[ValidateMerchantRequest] = None,
        *,
        origin: Optional[str] = None,
    ) -> dict[str, Any]:
        """Validate the merchant for an Apple Pay session and return the merchant session.
        ``POST /payments/{payment_id}/apple-pay/validate``

        :param params: ``{"validationUrl": ...}`` from the browser's
            ``onvalidatemerchant`` event.
        :param origin: The checkout page's origin (``https://shop.example``). It has to be
            registered for the eshop; an unregistered origin is answered with ``403``.
        """
        pid = require_path_segment(payment_id, "payment_id")
        headers = None if origin is None else {"Origin": require_https_origin(origin, "origin")}
        path = f"/payments/{pid}/apple-pay/validate"
        result = self._transport.request("POST", path, body=params, headers=headers)
        return cast("dict[str, Any]", result)

    @_reported
    def get_qr_payment_info(
        self, payment_id: str, format: Optional[Literal["png", "svg"]] = None
    ) -> QRPaymentDetails:
        """Return the recipient details and QR code images for a payment.
        ``GET /payments/{payment_id}/qr-payment/info``

        :param format: Image format of the QR codes, ``"png"`` (the API default) or ``"svg"``.
        """
        pid = require_path_segment(payment_id, "payment_id")
        path = f"/payments/{pid}/qr-payment/info"
        if format is not None:
            if format not in ("png", "svg"):
                raise GoPaySDKError(
                    '[GoPaySDK] format must be "png" or "svg".',
                    error_code=ErrorCode.INVALID_ARGUMENT,
                )
            path += f"?format={format}"
        return cast(QRPaymentDetails, self._transport.request("GET", path))

    @_reported
    def await_charge_state(
        self,
        payment_id: str,
        *,
        on_action_required: Optional[Callable[[str], object]] = None,
        on_state_change: Optional[Callable[[PaymentChargeStatusResponse], object]] = None,
        interval: float = 2.0,
        initial_timeout: float = 30.0,
        cancel: Optional[threading.Event] = None,
    ) -> PaymentChargeStatusResponse:
        """Poll a charge until it settles and return its final state.

        Returns on ``SUCCEEDED``. Raises :class:`GoPaySDKError` with ``CHARGE_FAILED`` on
        ``FAILED`` — the state, ``fail_reason`` included, is on ``charge_state`` — and
        with ``CHARGE_TIMEOUT`` if the charge has not left ``REQUESTED``/``PROCESSING``
        within ``initial_timeout`` seconds.

        When the bank asks for 3-D Secure the charge moves to ``ACTION_REQUIRED`` and
        ``on_action_required`` receives the redirect URL. **Send the customer there
        immediately**: the bank keeps the authentication open only briefly, and a late
        redirect finds the payment already abandoned. From that point the timeout no
        longer applies and polling continues until the customer is done.

        :param cancel: Set this event to stop polling; raises ``CHARGE_FAILED``.
        """
        pid = require_path_segment(payment_id, "payment_id")
        return _polling.await_charge(
            lambda: cast(
                PaymentChargeStatusResponse,
                self._transport.request("GET", f"/payments/{pid}/charge"),
            ),
            interval=_interval(interval),
            initial_timeout=_seconds(initial_timeout, "initial_timeout"),
            on_state_change=on_state_change,
            on_action_required=on_action_required,
            cancel=cancel,
        )

    @_reported
    def await_payment_status(
        self,
        payment_id: str,
        *,
        terminal_states: Optional[Collection[str]] = None,
        on_state_change: Optional[Callable[[PaymentDetails], object]] = None,
        interval: float = 3.0,
        timeout: Optional[float] = None,
        cancel: Optional[threading.Event] = None,
    ) -> PaymentDetails:
        """Poll a payment until it reaches a terminal state and return it.

        For QR and bank-transfer payments, which are confirmed at the payment level
        (``PAID``) rather than by a charge. The default terminal states are ``PAID``,
        ``CANCELED``, ``TIMEOUTED``, ``REFUNDED`` and ``PARTIALLY_REFUNDED``.

        No timeout by default — the API ends an unpaid payment on its own schedule. Pass
        ``timeout`` (seconds) for a client-side ceiling; it raises ``CHARGE_TIMEOUT``.
        """
        pid = require_path_segment(payment_id, "payment_id")
        return _polling.await_state(
            lambda: cast(PaymentDetails, self._transport.request("GET", f"/payments/{pid}")),
            what="Payment",
            terminal_states=_terminal(terminal_states, PAYMENT_TERMINAL_STATES),
            interval=_interval(interval),
            timeout=None if timeout is None else _seconds(timeout, "timeout"),
            on_state_change=on_state_change,
            cancel=cancel,
        )

    # ------------------------------------------------------------------- refunds

    @_reported
    def refund_payment(self, payment_id: str, params: RefundCreateRequest) -> RefundDetails:
        """Refund a payment, fully or partially. ``POST /payments/{payment_id}/refunds``

        The refund comes back ``REQUESTED`` — accepted, not settled. Follow it with
        :meth:`await_refund_state`.

        :param params: ``{"amount": ...}`` in the smallest currency unit.
        """
        pid = require_path_segment(payment_id, "payment_id")
        path = f"/payments/{pid}/refunds"
        return cast(RefundDetails, self._transport.request("POST", path, body=params))

    @_reported
    def list_refunds(self, payment_id: str) -> list[RefundDetails]:
        """List the refunds of a payment. ``GET /payments/{payment_id}/refunds``"""
        pid = require_path_segment(payment_id, "payment_id")
        path = f"/payments/{pid}/refunds"
        return cast("list[RefundDetails]", self._transport.request("GET", path))

    @_reported
    def get_refund(self, refund_id: str) -> RefundDetails:
        """Return one refund. ``GET /refunds/{refund_id}``"""
        rid = require_path_segment(refund_id, "refund_id")
        return cast(RefundDetails, self._transport.request("GET", f"/refunds/{rid}"))

    @_reported
    def await_refund_state(
        self,
        refund_id: str,
        *,
        terminal_states: Optional[Collection[str]] = None,
        on_state_change: Optional[Callable[[RefundDetails], object]] = None,
        interval: float = 3.0,
        timeout: Optional[float] = None,
        cancel: Optional[threading.Event] = None,
    ) -> RefundDetails:
        """Poll a refund until it settles and return it.

        Returns once the refund is ``SUCCESS`` or ``FAILED``. ``FAILED`` is returned, not
        raised: it is a legitimate outcome the caller has to inspect.
        """
        rid = require_path_segment(refund_id, "refund_id")
        return _polling.await_state(
            lambda: cast(RefundDetails, self._transport.request("GET", f"/refunds/{rid}")),
            what="Refund",
            terminal_states=_terminal(terminal_states, REFUND_TERMINAL_STATES),
            interval=_interval(interval),
            timeout=None if timeout is None else _seconds(timeout, "timeout"),
            on_state_change=on_state_change,
            cancel=cancel,
        )

    # --------------------------------------------------------------------- cards

    @_reported
    def get_card_details(self, card_id: str) -> PermanentCardTokenDetails:
        """Return a stored card token. ``GET /cards/tokens/{card_id}`` (scope ``card:read``)"""
        cid = require_path_segment(card_id, "card_id")
        return cast(
            PermanentCardTokenDetails, self._transport.request("GET", f"/cards/tokens/{cid}")
        )

    @_reported
    def delete_card(self, card_id: str) -> None:
        """Delete a stored card token. ``DELETE /cards/tokens/{card_id}`` (scope ``card:write``)"""
        cid = require_path_segment(card_id, "card_id")
        self._transport.request("DELETE", f"/cards/tokens/{cid}")

    @_reported
    def tokenize_encrypted_card(self, payload: str) -> PermanentCardTokenDetails:
        """Store a card from the encrypted payload the GoPay card form produced.
        ``POST /cards/tokens`` (scope ``card:write``)

        ``payload`` is the JWE the GoPay-hosted card form returns in the browser. Forward
        it to the server unmodified; the card data inside never passes through your code.
        """
        body: TokenCreateRequest = {"payload": require_non_empty_string(payload, "payload")}
        return cast(
            PermanentCardTokenDetails, self._transport.request("POST", "/cards/tokens", body=body)
        )

    # --------------------------------------------------------------------- links

    @_reported
    def create_payment_link(self, goid: Union[str, int], params: LinkCreateRequest) -> LinkDetails:
        """Create a payment link. ``POST /eshops/{goid}/links``

        A link stores payment data; no payment exists until a customer opens it. The
        response has two identifiers that cannot be derived from each other: ``id``, for
        :meth:`get_payment_link` and :meth:`disable_payment_link`, and ``url``, the
        address to share. Keep ``id`` if you intend to manage the link later.
        """
        path = f"/eshops/{_goid(goid)}/links"
        return cast(LinkDetails, self._transport.request("POST", path, body=params))

    @_reported
    def get_payment_link(self, goid: Union[str, int], link_id: str) -> LinkDetails:
        """Return a payment link. ``GET /eshops/{goid}/links/{link_id}``

        Expiry is evaluated on read: a link past ``expires_at`` comes back
        ``active: False`` with ``stop_reason: "EXPIRED"``.
        """
        lid = require_path_segment(link_id, "link_id")
        path = f"/eshops/{_goid(goid)}/links/{lid}"
        return cast(LinkDetails, self._transport.request("GET", path))

    @_reported
    def disable_payment_link(self, goid: Union[str, int], link_id: str) -> None:
        """Disable a payment link so it starts no further payments.
        ``DELETE /eshops/{goid}/links/{link_id}``

        Not a delete: the link stays readable with ``stop_reason: "FROM_API"``. Disabling
        an inactive link answers ``409`` — including a one-shot link that has been used,
        which keeps leading to the payment it created; cancel that payment instead.
        """
        lid = require_path_segment(link_id, "link_id")
        self._transport.request("DELETE", f"/eshops/{_goid(goid)}/links/{lid}")

    # --------------------------------------------------------------- recurrences

    @_reported
    def create_recurrence(
        self, goid: Union[str, int], params: RecurrenceCreateRequest
    ) -> RecurrenceDetails:
        """Create a recurrence. ``POST /eshops/{goid}/recurrences``

        Charges nothing and creates no payment: it stores a payment template and, for an
        ``AUTO`` recurrence, a schedule. :meth:`start_recurrence` creates the first
        payment. ``recurrence_date_to`` must be a plain ``YYYY-MM-DD`` date.
        """
        path = f"/eshops/{_goid(goid)}/recurrences"
        return cast(RecurrenceDetails, self._transport.request("POST", path, body=params))

    @_reported
    def get_recurrence(self, rec_id: str) -> RecurrenceDetails:
        """Return a recurrence and the payment it carries. ``GET /recurrences/{rec_id}``"""
        rid = require_path_segment(rec_id, "rec_id")
        return cast(RecurrenceDetails, self._transport.request("GET", f"/recurrences/{rid}"))

    @_reported
    def start_recurrence(
        self, rec_id: str, override: Optional[PaymentInstanceOverride] = None
    ) -> PaymentDetails:
        """Create the first payment of a recurrence. ``POST /recurrences/{rec_id}/start``

        The recurrence moves to ``REQUESTED``. The customer pays the returned payment at
        its ``gw_url`` — for recurrences that hosted gateway is the intended flow — and
        only then does the recurrence become ``STARTED``. Wait for that with
        :meth:`await_recurrence_state`.

        :param override: Per-payment overrides of the stored template. ``customer`` is
            merged field by field.
        """
        rid = require_path_segment(rec_id, "rec_id")
        path = f"/recurrences/{rid}/start"
        return cast(PaymentDetails, self._transport.request("POST", path, body=override))

    @_reported
    def create_next_payment(
        self, rec_id: str, override: Optional[PaymentInstanceOverride] = None
    ) -> PaymentDetails:
        """Create the next payment of a ``STARTED`` recurrence.
        ``POST /recurrences/{rec_id}/next``

        Answers ``409`` until the customer has paid the first payment.
        """
        rid = require_path_segment(rec_id, "rec_id")
        path = f"/recurrences/{rid}/next"
        return cast(PaymentDetails, self._transport.request("POST", path, body=override))

    @_reported
    def stop_recurrence(self, rec_id: str) -> None:
        """Stop a recurrence so it creates no further payments. ``DELETE /recurrences/{rec_id}``

        It stays readable as ``STOPPED`` with ``stop_reason: "CANCELLED_VIA_API"``. A
        recurrence whose first payment is still waiting to be paid cannot be stopped — the
        API answers ``409`` — so cancel that payment with :meth:`cancel_payment` first.
        """
        rid = require_path_segment(rec_id, "rec_id")
        self._transport.request("DELETE", f"/recurrences/{rid}")

    @_reported
    def await_recurrence_state(
        self,
        rec_id: str,
        *,
        terminal_states: Optional[Collection[str]] = None,
        on_state_change: Optional[Callable[[RecurrenceDetails], object]] = None,
        interval: float = 3.0,
        timeout: Optional[float] = None,
        cancel: Optional[threading.Event] = None,
    ) -> RecurrenceDetails:
        """Poll a recurrence until it is ``STARTED`` or ``STOPPED`` and return it.

        ``STOPPED`` is returned, not raised — inspect ``stop_reason``. With the default
        terminal states, a wait that can no longer end raises instead of polling forever:
        a recurrence still ``NEW`` (only :meth:`start_recurrence` moves it on) raises
        ``INVALID_ARGUMENT``, and one whose first payment was canceled or timed out raises
        ``CHARGE_FAILED``.
        """
        rid = require_path_segment(rec_id, "rec_id")
        check_reachable = terminal_states is None

        def poll() -> RecurrenceDetails:
            rec = cast(RecurrenceDetails, self._transport.request("GET", f"/recurrences/{rid}"))
            if check_reachable:
                _assert_recurrence_can_settle(rec)
            return rec

        return _polling.await_state(
            poll,
            what="Recurrence",
            terminal_states=_terminal(terminal_states, RECURRENCE_TERMINAL_STATES),
            interval=_interval(interval),
            timeout=None if timeout is None else _seconds(timeout, "timeout"),
            on_state_change=on_state_change,
            cancel=cancel,
        )


def _goid(goid: Union[str, int]) -> str:
    if isinstance(goid, int) and not isinstance(goid, bool):
        goid = str(goid)
    return require_path_segment(goid, "goid")


def _interval(value: Any) -> float:
    seconds = _seconds(value, "interval")
    if seconds < MIN_POLL_INTERVAL:
        raise GoPaySDKError(
            f"[GoPaySDK] interval must be at least {MIN_POLL_INTERVAL:g} second",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
    return seconds


def _terminal(states: Optional[Collection[str]], default: Collection[str]) -> Collection[str]:
    if states is None:
        return default
    # A bare string is a Collection[str] too, of its characters: "PAID" would wait for
    # "P", "A", "I" or "D" and never end.
    if isinstance(states, str) or not all(isinstance(state, str) for state in states):
        raise GoPaySDKError(
            '[GoPaySDK] terminal_states must be a collection of states, e.g. ["PAID"]',
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
    return states


def _seconds(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise GoPaySDKError(
            f"[GoPaySDK] {field} must be a non-negative number of seconds",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
    return float(value)


def _assert_recurrence_can_settle(rec: RecurrenceDetails) -> None:
    state = rec.get("state")
    if state == "NEW":
        raise GoPaySDKError(
            "[GoPaySDK] Recurrence is still NEW — call start_recurrence() first, "
            "nothing else moves it out of this state.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
    payment = rec.get("payment")
    payment_state = payment.get("state") if isinstance(payment, dict) else None
    if state == "REQUESTED" and payment_state in UNPAYABLE_PAYMENT_STATES:
        raise GoPaySDKError(
            f"[GoPaySDK] The recurrence's first payment is {payment_state}, so the "
            "recurrence can no longer reach STARTED.",
            error_code=ErrorCode.CHARGE_FAILED,
        )
