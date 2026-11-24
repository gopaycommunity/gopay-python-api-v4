from __future__ import annotations

import base64
import datetime
import email.utils
import hashlib
import json
import logging
import random
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import httpx

from . import _correlation
from ._correlation import RequestIds
from ._endpoint import normalize_endpoint
from .environment import Environment, resolve_base_url
from .errors import ErrorCode, GoPayError, GoPayHTTPError, GoPaySDKError
from .token_cache import CachedToken, InMemoryTokenCache, TokenCache

logger = logging.getLogger("gopay_payments")

# A path, not a secret: the S105 check takes any *TOKEN* name for one.
TOKEN_PATH = "/oauth2/token"  # noqa: S105
JSON_CONTENT_TYPE = "application/json"
ACCEPT = JSON_CONTENT_TYPE

# A token this close to expiry is replaced before use rather than sent and rejected.
REFRESH_MARGIN_SECONDS = 30.0

# Retries are for failures the request itself did not cause — a 5xx, a dropped connection,
# a 429 — and only for reads. A POST is never retried: a charge or a refund that reached the
# API must not be sent twice. Nor is a DELETE: cancel, stop and disable answer 409 to a
# repeat, so retrying one the API had already carried out would turn its success into an
# error.
MAX_RETRIES = 2
RETRY_BASE_DELAY_SECONDS = 0.2
RETRIED_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# A 429 asking for a longer wait is handed to the caller rather than blocking the call.
MAX_RETRY_AFTER_SECONDS = 10.0
# ASCII: a bare \d would also take digits of other scripts.
_SECONDS = re.compile(r"\d+", re.ASCII)

OnError = Callable[[GoPayError], object]


# Anyone who can reach a webhook endpoint can cause these, so monitoring must not hear of
# them. A key fetch that failed is reported, once per attempt, where it happens.
NEVER_REPORTED = frozenset({ErrorCode.WEBHOOK_INVALID, ErrorCode.WEBHOOK_KEYS_UNAVAILABLE})


def report(on_error: Optional[OnError], error: GoPayError) -> None:
    """Hand ``error`` to ``on_error``, once, whatever happens inside the callback."""
    if error._reported:
        return
    error._reported = True
    if on_error is None or (
        isinstance(error, GoPaySDKError) and error.error_code in NEVER_REPORTED
    ):
        return
    try:
        on_error(error)
    except Exception:
        # The integrator's monitoring failing must not replace the SDK's own error.
        logger.warning(
            "on_error callback raised; the original error is raised regardless", exc_info=True
        )


@dataclass(frozen=True)
class _Credentials:
    client_id: str
    # Out of the repr: credentials sit in the locals of every frame that handles them, and
    # tracebacks and error trackers print those.
    client_secret: str = field(repr=False)
    scope: str


class Transport:
    """HTTP plumbing shared by every API method: auth, retries, error mapping."""

    def __init__(
        self,
        *,
        environment: Environment,
        base_url: Optional[str],
        timeout: float,
        user_agent: str,
        http_client: Optional[httpx.Client],
        token_cache: Optional[TokenCache],
        on_error: Optional[OnError],
    ) -> None:
        self._on_error = on_error
        self.environment = environment
        self.base_url = resolve_base_url(environment, base_url)
        self.user_agent = user_agent
        self._timeout = timeout
        self._owns_http = http_client is None
        self._http = http_client if http_client is not None else httpx.Client()
        self._cache: TokenCache = token_cache if token_cache is not None else InMemoryTokenCache()
        self._credentials: Optional[_Credentials] = None
        # Serialises token refreshes: concurrent callers that all find the token expiring
        # wait for one refresh and reuse its result instead of each requesting a token.
        self._lock = threading.RLock()
        # Indirection so tests can run retries without real delays.
        self._sleep: Callable[[float], None] = time.sleep
        self._clock: Callable[[], float] = time.time
        # Not for anything secret: it only spreads retries apart.
        self._random: Callable[[], float] = random.random

    # ------------------------------------------------------------------ reporting

    def report(self, error: GoPayError) -> None:
        report(self._on_error, error)

    # ---------------------------------------------------------------- credentials

    @property
    def client_id(self) -> Optional[str]:
        credentials = self._credentials
        return credentials.client_id if credentials is not None else None

    def _cache_key(self, credentials: _Credentials) -> str:
        # Environment, client and scope identify a token. The secret does not belong in a
        # key that a shared cache may log or expose.
        material = "\n".join((self.base_url, credentials.client_id, credentials.scope))
        return "gopay-payments-v4:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]

    def _is_usable(self, token: Optional[CachedToken]) -> bool:
        return token is not None and self._clock() < token.expires_at - REFRESH_MARGIN_SECONDS

    def authenticate(self, client_id: str, client_secret: str, scope: str) -> None:
        credentials = _Credentials(client_id, client_secret, scope)
        with self._lock:
            self._credentials = credentials
            if self._is_usable(self._cache.get(self._cache_key(credentials))):
                return
            try:
                self._fetch_token(credentials)
            except GoPayError as error:
                # The same rule as for a refresh: only a refusal of the credentials is a
                # reason to forget them. After a timeout or a 5xx they stay, and the next
                # call obtains the token instead of failing with AUTH_TOKEN_MISSING.
                if _is_permanent_rejection(error):
                    self._forget(credentials)
                raise

    def has_token(self) -> bool:
        credentials = self._credentials
        if credentials is None:
            return False
        return self._cache.get(self._cache_key(credentials)) is not None

    def logout(self) -> None:
        with self._lock:
            credentials, self._credentials = self._credentials, None
            if credentials is not None:
                self._cache.delete(self._cache_key(credentials))

    def _forget(self, credentials: _Credentials) -> None:
        # Credentials the API has turned down are not retried with every later call.
        with self._lock:
            if self._credentials == credentials:
                self._credentials = None
            self._cache.delete(self._cache_key(credentials))

    def _fetch_token(self, credentials: _Credentials) -> CachedToken:
        # Named so that error trackers scrubbing locals by name ("authorization") hide it.
        authorization = "Basic " + base64.b64encode(
            f"{credentials.client_id}:{credentials.client_secret}".encode()
        ).decode("ascii")
        ids = _correlation.request_ids()
        response = self._send(
            "POST",
            TOKEN_PATH,
            headers={
                "Accept": ACCEPT,
                "User-Agent": self.user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": authorization,
                **_correlation.headers_for(ids, TOKEN_PATH),
            },
            content=urlencode(
                {"grant_type": "client_credentials", "scope": credentials.scope}
            ).encode(),
            ids=ids,
        )
        if not response.is_success:
            raise _correlation.tag(
                GoPayHTTPError(
                    response.status_code,
                    _error_body(response),
                    method="POST",
                    endpoint=TOKEN_PATH,
                    retry_after=_retry_after(response, self._clock()),
                ),
                ids,
            )
        try:
            body = response.json()
        except ValueError:
            # Not chained: the decode error keeps the whole body, which may hold a token.
            raise _correlation.tag(
                GoPaySDKError(
                    "[GoPaySDK] Invalid token response: could not parse JSON.",
                    error_code=ErrorCode.AUTH_INVALID_RESPONSE,
                ),
                ids,
            ) from None
        access_token = body.get("access_token") if isinstance(body, dict) else None
        expires_in = body.get("expires_in") if isinstance(body, dict) else None
        if (
            not isinstance(access_token, str)
            or not access_token
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
        ):
            raise _correlation.tag(
                GoPaySDKError(
                    "[GoPaySDK] Invalid token response: missing required fields.",
                    error_code=ErrorCode.AUTH_INVALID_RESPONSE,
                ),
                ids,
            )
        token = CachedToken(access_token=access_token, expires_at=self._clock() + float(expires_in))
        self._cache.set(self._cache_key(credentials), token)
        return token

    def _bearer(self) -> str:
        credentials = self._credentials
        if credentials is not None:
            token = self._cache.get(self._cache_key(credentials))
            if token is not None and self._is_usable(token):
                return token.access_token
        with self._lock:
            credentials = self._credentials
            if credentials is None:
                raise GoPaySDKError(
                    "[GoPaySDK] No access token available. Call authenticate() first.",
                    error_code=ErrorCode.AUTH_TOKEN_MISSING,
                )
            # Another thread may have refreshed while this one waited for the lock.
            token = self._cache.get(self._cache_key(credentials))
            if token is not None and self._is_usable(token):
                return token.access_token
            try:
                return self._fetch_token(credentials).access_token
            except GoPayError as cause:
                # Only a refusal of the credentials themselves is worth forgetting them
                # for. A timeout, a dropped connection or a 5xx says nothing about them,
                # and forgetting them would leave a long-running worker unable to recover
                # until someone calls authenticate() again.
                if _is_permanent_rejection(cause):
                    self._forget(credentials)
                    raise _like(
                        cause,
                        GoPaySDKError(
                            "[GoPaySDK] Token refresh failed: the credentials were rejected. "
                            "Call authenticate() again.",
                            error_code=ErrorCode.AUTH_REFRESH_FAILED,
                        ),
                    ) from cause
                raise _like(
                    cause,
                    GoPaySDKError(
                        "[GoPaySDK] Token refresh failed. The credentials are kept and the next "
                        "request tries again.",
                        error_code=ErrorCode.AUTH_REFRESH_FAILED,
                    ),
                ) from cause

    def _discard(self, rejected: str) -> None:
        # Drop the token the API just rejected — unless another thread has already put a
        # newer one in its place, which a blind delete would throw away.
        credentials = self._credentials
        if credentials is None:
            return
        key = self._cache_key(credentials)
        with self._lock:
            current = self._cache.get(key)
            if current is not None and current.access_token == rejected:
                self._cache.delete(key)

    # ------------------------------------------------------------------ requests

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        many: bool = False,
        read: Optional[Callable[[Any], Any]] = None,
        retry: bool = True,
    ) -> Any:
        """Send an authenticated API request and return the JSON object it answered with.

        ``body=None`` sends no body. With ``many=True`` the answer is a list of objects. Any
        other 2xx body — empty included — raises ``UNEXPECTED_RESPONSE``: a method typed to
        return ``PaymentDetails`` must not hand its caller ``None``.

        ``read`` checks and converts the decoded body. An error it raises carries this
        request's ids, as any other error of the request does. ``retry=False`` sends a read
        once, for a caller that must not wait out retries.
        """
        return self.fetch(method, path, body=body, many=many, read=read, retry=retry)[0]

    def fetch(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        many: bool = False,
        read: Optional[Callable[[Any], Any]] = None,
        retry: bool = True,
    ) -> tuple[Any, RequestIds]:
        """:meth:`request`, also returning the ids the request went out with — for an error
        found in the answer only later, which must still point at this request."""
        response, ids = self._exchange(method, path, body=body, retry=retry)
        try:
            data = _json_body(response, many=many)
            return (read(data) if read is not None else data), ids
        except GoPayError as error:
            _correlation.tag(error, ids)
            raise

    def delete(self, path: str) -> None:
        """Send an authenticated ``DELETE``. The API answers ``204``; there is nothing to return."""
        self._exchange("DELETE", path, body=None)

    def _exchange(
        self,
        method: str,
        path: str,
        *,
        body: Any,
        retry: bool = True,
    ) -> tuple[httpx.Response, RequestIds]:
        content = _encode(body)
        # One trace id for the request, kept by every attempt: the 5xx retries and the resend
        # after a 401 are the same request, and the gateway's log groups them by it.
        ids = _correlation.request_ids()

        token = self._bearer()
        response = self._send(
            method,
            path,
            headers=self._headers(method, path, token, ids),
            content=content,
            ids=ids,
            retry=retry,
        )

        if response.status_code == 401:
            self._discard(token)
            token = self._bearer()
            response = self._send(
                method,
                path,
                headers=self._headers(method, path, token, ids),
                content=content,
                ids=ids,
                retry=False,
            )
            if response.status_code == 401:
                # The credentials stay: the fresh token they produced was rejected by this
                # endpoint, which says nothing about the others. The token goes, so the
                # next call starts from a new one.
                self._discard(token)
                raise _correlation.tag(
                    GoPaySDKError(
                        "[GoPaySDK] Request unauthorized after token refresh. Check OAuth2 scopes.",
                        error_code=ErrorCode.AUTH_UNAUTHORIZED,
                    ),
                    ids,
                )

        if not response.is_success:
            raise _correlation.tag(
                GoPayHTTPError(
                    response.status_code,
                    _error_body(response),
                    method=method,
                    endpoint=normalize_endpoint(path),
                    retry_after=_retry_after(response, self._clock()),
                ),
                ids,
            )
        return response, ids

    def _headers(self, method: str, path: str, token: str, ids: RequestIds) -> dict[str, str]:
        headers = {
            "Accept": ACCEPT,
            "User-Agent": self.user_agent,
            "Authorization": f"Bearer {token}",
            **_correlation.headers_for(ids, path),
        }
        if method in ("POST", "PUT"):
            # Also on a POST without a body: the gateway answers 415 to a POST that does
            # not declare a content type, even when the endpoint takes no body at all.
            headers["Content-Type"] = JSON_CONTENT_TYPE
        return headers

    def _send(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        content: Optional[bytes],
        ids: RequestIds,
        retry: bool = True,
    ) -> httpx.Response:
        url = self.base_url + path
        attempts = 1 + (MAX_RETRIES if retry and method in RETRIED_METHODS else 0)
        last_error: Optional[Exception] = None
        # What a 429 asked to wait before the next attempt; None backs off as usual.
        wait: Optional[float] = None
        for attempt in range(attempts):
            if attempt:
                self._sleep(wait if wait is not None else self._backoff(attempt))
            try:
                response = self._attempt(method, url, headers, content, ids)
            except httpx.RequestError as error:
                last_error, wait = error, None
                continue
            if attempt == attempts - 1:
                return response
            again, wait = self._retry_decision(response)
            if not again:
                return response
        raise _correlation.tag(
            GoPaySDKError(
                f"[GoPaySDK] Network error ({type(last_error).__name__}).",
                error_code=ErrorCode.NETWORK_ERROR,
            ),
            ids,
        ) from last_error

    def _attempt(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        content: Optional[bytes],
        ids: RequestIds,
    ) -> httpx.Response:
        """One HTTP attempt. A timeout raises ``NETWORK_TIMEOUT``; a dropped connection
        propagates as ``httpx.RequestError`` for the caller to retry."""
        # The trace id on every line, so a merchant's log points GoPay support at the
        # request — a timeout and a dropped connection included, where no answer came.
        logger.debug("→ %s %s trace_id=%s", method, url, ids.trace_id)
        try:
            response = self._http.request(
                method, url, headers=headers, content=content, timeout=self._timeout
            )
        except httpx.TimeoutException as error:
            logger.debug("✗ %s %s timed out trace_id=%s", method, url, ids.trace_id)
            # Not retried: a timed-out request may still be running on the other side.
            raise _correlation.tag(
                GoPaySDKError(
                    "[GoPaySDK] Request timed out.", error_code=ErrorCode.NETWORK_TIMEOUT
                ),
                ids,
            ) from error
        except httpx.RequestError as error:
            logger.debug("✗ %s %s %s trace_id=%s", method, url, type(error).__name__, ids.trace_id)
            raise
        logger.debug("← %s %s trace_id=%s", response.status_code, url, ids.trace_id)
        return response

    def _retry_decision(self, response: httpx.Response) -> tuple[bool, Optional[float]]:
        """Whether a read is worth another attempt, and the wait a 429 asked for."""
        if response.status_code == 429:
            wait = _retry_after(response, self._clock())
            # A longer wait goes to the caller rather than blocking the call.
            return wait is None or wait <= MAX_RETRY_AFTER_SECONDS, wait
        return response.status_code >= 500, None

    def _backoff(self, attempt: int) -> float:
        """Exponential, with jitter: clients that failed together do not retry together."""
        ceiling = RETRY_BASE_DELAY_SECONDS * float(2 ** (attempt - 1))
        return ceiling / 2 + self._random() * ceiling / 2

    def close(self) -> None:
        if self._owns_http:
            self._http.close()


def _encode(body: Any) -> Optional[bytes]:
    if body is None:
        return None
    try:
        # allow_nan=False: NaN and Infinity are not JSON, so they never reach the wire.
        return json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise GoPaySDKError(
            f"[GoPaySDK] The request body cannot be sent as JSON ({error}). "
            "Dates go as ISO 8601 strings, e.g. date.isoformat().",
            error_code=ErrorCode.INVALID_ARGUMENT,
        ) from error


def _json_body(response: httpx.Response, *, many: bool) -> Any:
    expected = "a JSON list of objects" if many else "a JSON object"
    try:
        data = response.json()  # an empty body raises too
    except ValueError as error:
        # Chained, unlike the token response in _fetch_token: an API response holds no bearer
        # token, and the decode error is what tells a developer what the API sent.
        raise _unexpected_response(expected) from error
    if many:
        valid = isinstance(data, list) and all(isinstance(item, dict) for item in data)
    else:
        valid = isinstance(data, dict)
    if not valid:
        raise _unexpected_response(expected)
    return data


def _unexpected_response(expected: str) -> GoPaySDKError:
    return GoPaySDKError(
        f"[GoPaySDK] Failed to parse API response as {expected}.",
        error_code=ErrorCode.UNEXPECTED_RESPONSE,
    )


def _retry_after(response: httpx.Response, now: float) -> Optional[float]:
    """The wait ``Retry-After`` asks for in seconds, or ``None`` when there is none to read."""
    value = response.headers.get("retry-after", "").strip()
    if not value:
        return None
    if _SECONDS.fullmatch(value):
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        # RFC 9110 dates are GMT; parsedate_to_datetime leaves "-0000" naive.
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, float(when.timestamp()) - now)


def _like(cause: GoPayError, error: GoPayError) -> GoPayError:
    """``error`` carrying the request ids of ``cause``, the failure it stands in for."""
    error.trace_id = cause.trace_id
    error.transaction_id = cause.transaction_id
    return error


def _is_permanent_rejection(error: GoPayError) -> bool:
    """Whether the token endpoint turned the credentials down, rather than failing for now."""
    return (
        isinstance(error, GoPayHTTPError)
        and 400 <= error.status < 500
        and error.status not in (408, 429)
    )


def _error_body(response: httpx.Response) -> Any:
    # application/json, and the +json types an intermediary may answer with
    # (application/problem+json).
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == JSON_CONTENT_TYPE or content_type.endswith("+json"):
        try:
            return response.json()
        except ValueError:
            pass
    return response.text
