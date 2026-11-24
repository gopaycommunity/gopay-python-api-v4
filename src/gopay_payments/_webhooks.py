"""Webhook verification: is the request from GoPay, and which event does it carry.

``X-GoPay-Signature`` is a detached JWS (RFC 7515 Appendix F) — ``<protected header>..<signature>``
— signed RS256 over the body exactly as sent. The keys come from ``GET /webhooks/public-keys``
and are kept by ``kid``.

RSA needs the ``cryptography`` package, an optional extra (``gopay-payments-v4[webhooks]``):
an integration that never receives webhooks should not have to install it.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional, Union, cast

from ._events import GoPayEvent, is_event
from .errors import ErrorCode, GoPayError, GoPayHTTPError, GoPaySDKError

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

    from ._transport import Transport

PUBLIC_KEYS_PATH = "/webhooks/public-keys"

# An unknown kid refetches the keys at most this often: anyone can send a request naming a
# made-up key, and each one must not become a call to GoPay. Short, because a real webhook
# signed with a newly rotated-in key waits it out as a 5xx GoPay retries — and a failed
# refresh is tried again after it, not after another hour of trusting the old set.
REFETCH_INTERVAL_SECONDS = 10.0
# Older keys are refetched before use, so a key GoPay withdraws stops verifying.
KEYS_MAX_AGE_SECONDS = 3600.0

WebhookBody = Union[bytes, bytearray, memoryview, str]
"""The request body exactly as received: Django ``request.body``, Flask ``request.get_data()``."""

SignatureHeader = Union[str, Sequence[str], None]
"""The ``X-GoPay-Signature`` header: its value, or the list a framework collects repeats into."""

_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class _Signature:
    # The protected header exactly as sent: it is part of the signing input.
    protected_header: str
    kid: str
    signature: bytes


# Messages carry only text this SDK wrote: everything in the request came from whoever sent it.
def _invalid(reason: str) -> GoPaySDKError:
    return GoPaySDKError(
        f"[GoPaySDK] Not a valid GoPay webhook: {reason}.",
        error_code=ErrorCode.WEBHOOK_INVALID,
    )


def _b64url_decode(value: Any) -> Optional[bytes]:
    # Strict: unpadded base64url only. fullmatch, so a trailing line break is refused too.
    if not isinstance(value, str) or not _BASE64URL.fullmatch(value) or len(value) % 4 == 1:
        return None
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return None


def _b64url_encode(data: bytes) -> str:
    """Unpadded, as the JWS signing input requires (RFC 7515 §2)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def body_bytes(raw_body: Any) -> bytes:
    if isinstance(raw_body, str):
        return raw_body.encode("utf-8")
    if isinstance(raw_body, (bytes, bytearray, memoryview)):
        return bytes(raw_body)
    # The usual cause is a parsed body: request.json in Flask, json.loads in Django. The
    # signature covers the bytes, which are gone by then.
    raise GoPaySDKError(
        "[GoPaySDK] raw_body must be the request body as received (bytes or str) — Django "
        "request.body, Flask request.get_data() — not parsed JSON",
        error_code=ErrorCode.INVALID_ARGUMENT,
    )


def parse_signature_header(header: Any) -> _Signature:
    """Read a detached JWS whose header is exactly ``{"alg":"RS256","kid":…}``.

    Anything else is refused rather than guessed at.
    """
    value: Optional[str] = None
    if isinstance(header, str):
        value = header
    elif (
        isinstance(header, Sequence)
        and not isinstance(header, (bytes, bytearray))
        and len(header) == 1
        and isinstance(header[0], str)
    ):
        value = header[0]
    if not value:
        raise _invalid("exactly one X-GoPay-Signature header is required")

    parts = value.split(".")
    if len(parts) != 3 or parts[1] != "":
        raise _invalid("X-GoPay-Signature is not a detached JWS")
    protected_header, _, signature_part = parts
    header_bytes = _b64url_decode(protected_header)
    signature = _b64url_decode(signature_part)
    if header_bytes is None or signature is None:
        raise _invalid("X-GoPay-Signature is not base64url")

    try:
        jose = json.loads(header_bytes.decode("utf-8"))
    except ValueError:
        raise _invalid("the JWS header is not JSON") from None
    if not isinstance(jose, dict) or jose.get("alg") != "RS256":
        raise _invalid("the JWS header is not RS256")
    kid = jose.get("kid")
    if not isinstance(kid, str) or not kid:
        raise _invalid("the JWS header names no key")
    # `crit` lists extensions a verifier must understand or refuse (RFC 7515 §4.1.11), and
    # `b64: false` (RFC 7797) would change the signing input; GoPay sends neither.
    if "crit" in jose or "b64" in jose:
        raise _invalid("the JWS header uses an unsupported extension")
    return _Signature(protected_header, kid, signature)


def parse_event(body: bytes) -> GoPayEvent:
    """The event in a verified body, checked for the fields every event has.

    A ``type`` this SDK version does not know comes back unchanged, never as an error.
    """
    try:
        event = json.loads(body.decode("utf-8"))
    except ValueError:
        raise _invalid("the body is not JSON") from None
    if not is_event(event):
        raise _invalid("the body is not an event")
    return event


def require_crypto() -> None:
    """Fail with a clear error when the ``webhooks`` extra is not installed."""
    try:
        import cryptography.hazmat.primitives.asymmetric.rsa  # noqa: F401
    except ImportError as error:
        raise GoPaySDKError(
            "[GoPaySDK] Verifying webhooks needs the cryptography package: "
            "pip install 'gopay-payments-v4[webhooks]'",
            error_code=ErrorCode.INVALID_CONFIG,
        ) from error


def _public_key(jwk: Any) -> Optional[RSAPublicKey]:
    from cryptography.hazmat.primitives.asymmetric import rsa

    if (
        not isinstance(jwk, dict)
        or jwk.get("kty") != "RSA"
        or jwk.get("alg") != "RS256"
        or jwk.get("use") != "sig"
    ):
        return None
    modulus = _b64url_decode(jwk.get("n"))
    exponent = _b64url_decode(jwk.get("e"))
    if not modulus or not exponent:
        return None
    try:
        return rsa.RSAPublicNumbers(
            int.from_bytes(exponent, "big"), int.from_bytes(modulus, "big")
        ).public_key()
    except (ValueError, TypeError):
        # One malformed key must not take the others down with it.
        return None


def _verify(key: RSAPublicKey, signature: _Signature, body: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    signing_input = f"{signature.protected_header}.{_b64url_encode(body)}".encode("ascii")
    try:
        key.verify(signature.signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError):
        return False
    return True


def _read_keys(data: Any) -> dict[str, RSAPublicKey]:
    keys: dict[str, RSAPublicKey] = {}
    listed = data.get("keys") if isinstance(data, dict) else None
    for jwk in listed if isinstance(listed, list) else []:
        kid = jwk.get("kid") if isinstance(jwk, dict) else None
        key = _public_key(jwk) if isinstance(kid, str) else None
        if kid is not None and key is not None:
            keys[kid] = key
    # A set with no key to verify with is not GoPay saying every webhook is forged. Taken as
    # one, it would replace a good set, and every webhook waiting on this fetch would get
    # WEBHOOK_INVALID, a 4xx GoPay does not retry.
    if not keys:
        raise GoPaySDKError(
            "[GoPaySDK] GET /webhooks/public-keys returned no usable RS256 signing key.",
            error_code=ErrorCode.UNEXPECTED_RESPONSE,
        )
    return keys


def _keys_unavailable(cause: Optional[GoPayError]) -> GoPaySDKError:
    if cause is None:
        return GoPaySDKError(
            "[GoPaySDK] The webhook names a signing key the SDK has not loaded, and the keys "
            "were fetched moments ago, so it was not verified yet.",
            error_code=ErrorCode.WEBHOOK_KEYS_UNAVAILABLE,
        )
    scope_refused = (isinstance(cause, GoPayHTTPError) and cause.status == 403) or (
        isinstance(cause, GoPaySDKError) and cause.error_code is ErrorCode.AUTH_UNAUTHORIZED
    )
    hint = " The access token needs the shared:read scope." if scope_refused else ""
    return GoPaySDKError(
        f"[GoPaySDK] The webhook signing keys could not be loaded, so the webhook was not "
        f"verified.{hint}",
        error_code=ErrorCode.WEBHOOK_KEYS_UNAVAILABLE,
    )


class Webhooks:
    """Verifies webhooks against GoPay's signing keys, kept by ``kid`` for the client's life."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._keys: dict[str, RSAPublicKey] = {}
        # Last success and last attempt apart: a failed refresh is retried after the short
        # interval, not after another hour of trusting the old set.
        self._fetched_at: Optional[float] = None
        self._attempted_at: Optional[float] = None
        self._last_failure: Optional[GoPayError] = None
        # Held across the fetch: webhooks arriving together wait for one fetch and share it.
        self._lock = threading.Lock()
        self._clock: Callable[[], float] = time.monotonic

    def verify(self, raw_body: Any, signature_header: Any) -> GoPayEvent:
        require_crypto()
        body = body_bytes(raw_body)
        signature = parse_signature_header(signature_header)
        key = self._key_for(signature.kid)
        if key is None:
            raise _invalid("it is signed with a key GoPay does not publish")
        if not _verify(key, signature, body):
            raise _invalid("the signature does not match the body")
        return parse_event(body)

    def _key_for(self, kid: str) -> Optional[RSAPublicKey]:
        """The key, or ``None`` when GoPay's current keys do not include it.

        Raises when that could not be established: not checked is not forged, and GoPay
        retries only a 5xx.
        """
        with self._lock:
            now = self._clock()
            due = (
                kid not in self._keys
                or self._fetched_at is None
                or now - self._fetched_at >= KEYS_MAX_AGE_SECONDS
            )
            checked = False
            if due and (
                self._attempted_at is None or now - self._attempted_at >= REFETCH_INTERVAL_SECONDS
            ):
                self._load()
                checked = True
            key = self._keys.get(kid)
            if key is not None:
                return key
            if self._last_failure is not None:
                raise _keys_unavailable(self._last_failure) from self._last_failure
            if not checked:
                raise _keys_unavailable(None)
            return None

    def _load(self) -> None:
        """Replace the keys as a whole on success; a failed fetch keeps the last set."""
        try:
            keys = self._transport.request("GET", PUBLIC_KEYS_PATH, read=_read_keys)
            self._keys = cast("dict[str, RSAPublicKey]", keys)
            self._last_failure = None
            self._fetched_at = self._clock()
        except GoPayError as error:
            # Reported here, once for this attempt. The webhooks that find the failure
            # standing raise WEBHOOK_KEYS_UNAVAILABLE, which is never reported, so an endpoint
            # that keeps failing does not turn every webhook into a report.
            self._transport.report(error)
            self._last_failure = error
        finally:
            self._attempted_at = self._clock()
