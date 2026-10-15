from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
from collections.abc import Mapping
from typing import Any, Callable, NamedTuple, Optional
from urllib.parse import urlencode

import httpx

from ._endpoint import normalize_endpoint
from .environment import Environment, resolve_base_url
from .errors import ErrorCode, GoPayError, GoPayHTTPError, GoPaySDKError
from .token_cache import CachedToken, InMemoryTokenCache, TokenCache

logger = logging.getLogger("gopay_payments")

TOKEN_PATH = "/oauth2/token"
JSON_CONTENT_TYPE = "application/json"
ACCEPT = JSON_CONTENT_TYPE

# A token this close to expiry is replaced before use rather than sent and rejected.
REFRESH_MARGIN_SECONDS = 30.0

# Retries are for failures the request itself did not cause — a 5xx or a dropped
# connection — and only for reads. A POST is never retried: a charge or a refund that
# reached the API must not be sent twice. Nor is a DELETE: cancel, stop and disable answer
# 409 to a repeat, so retrying one the API had already carried out would turn its success
# into an error.
MAX_RETRIES = 2
RETRY_BASE_DELAY_SECONDS = 0.2
RETRIED_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Set by the transport alone. Per-request headers cannot replace them.
RESERVED_HEADERS = frozenset({"accept", "authorization", "content-type", "user-agent"})

OnError = Callable[[GoPayError], object]


def report(on_error: Optional[OnError], error: GoPayError) -> None:
    """Hand ``error`` to ``on_error``, once, whatever happens inside the callback."""
    if error._reported:
        return
    error._reported = True
    if on_error is None:
        return
    try:
        on_error(error)
    except Exception:
        # The integrator's monitoring failing must not replace the SDK's own error.
        logger.warning(
            "on_error callback raised; the original error is raised regardless", exc_info=True
        )


class _Credentials(NamedTuple):
    client_id: str
    client_secret: str
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
            except GoPayError:
                self._credentials = None
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
        basic = base64.b64encode(
            f"{credentials.client_id}:{credentials.client_secret}".encode()
        ).decode("ascii")
        response = self._send(
            "POST",
            TOKEN_PATH,
            headers={
                "Accept": ACCEPT,
                "User-Agent": self.user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {basic}",
            },
            content=urlencode(
                {"grant_type": "client_credentials", "scope": credentials.scope}
            ).encode(),
        )
        if not response.is_success:
            raise GoPayHTTPError(
                response.status_code, _error_body(response), method="POST", endpoint=TOKEN_PATH
            )
        body = _json(response)
        access_token = body.get("access_token") if isinstance(body, dict) else None
        expires_in = body.get("expires_in") if isinstance(body, dict) else None
        if (
            not isinstance(access_token, str)
            or not access_token
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
        ):
            raise GoPaySDKError(
                "[GoPaySDK] Invalid token response: missing required fields.",
                error_code=ErrorCode.AUTH_INVALID_RESPONSE,
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
                raise GoPaySDKError(
                    "[GoPaySDK] Token refresh failed. Call authenticate() again.",
                    error_code=ErrorCode.AUTH_REFRESH_FAILED,
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
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        """Send an authenticated API request and return the parsed JSON body.

        ``body=None`` sends no body. Returns ``None`` for a response without content.
        """
        content = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")

        token = self._bearer()
        response = self._send(
            method, path, headers=self._headers(method, token, headers), content=content
        )

        if response.status_code == 401:
            self._discard(token)
            token = self._bearer()
            response = self._send(
                method,
                path,
                headers=self._headers(method, token, headers),
                content=content,
                retry=False,
            )
            if response.status_code == 401:
                # The credentials stay: the fresh token they produced was rejected by this
                # endpoint, which says nothing about the others. The token goes, so the
                # next call starts from a new one.
                self._discard(token)
                raise GoPaySDKError(
                    "[GoPaySDK] Request unauthorized after token refresh. Check OAuth2 scopes.",
                    error_code=ErrorCode.AUTH_UNAUTHORIZED,
                )

        if not response.is_success:
            raise GoPayHTTPError(
                response.status_code,
                _error_body(response),
                method=method,
                endpoint=normalize_endpoint(path),
            )
        if response.status_code == 204 or not response.content:
            return None
        return _json(response)

    def _headers(
        self, method: str, token: str, extra: Optional[Mapping[str, str]]
    ) -> dict[str, str]:
        headers = {k: v for k, v in (extra or {}).items() if k.lower() not in RESERVED_HEADERS}
        headers["Accept"] = ACCEPT
        headers["User-Agent"] = self.user_agent
        headers["Authorization"] = f"Bearer {token}"
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
        retry: bool = True,
    ) -> httpx.Response:
        url = self.base_url + path
        attempts = 1 + (MAX_RETRIES if retry and method in RETRIED_METHODS else 0)
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            if attempt:
                self._sleep(RETRY_BASE_DELAY_SECONDS * 2 ** (attempt - 1))
            logger.debug("→ %s %s", method, url)
            try:
                response = self._http.request(
                    method, url, headers=headers, content=content, timeout=self._timeout
                )
            except httpx.TimeoutException as error:
                # Not retried: a timed-out request may still be running on the other side.
                raise GoPaySDKError(
                    "[GoPaySDK] Request timed out.", error_code=ErrorCode.NETWORK_TIMEOUT
                ) from error
            except httpx.RequestError as error:
                last_error = error
                continue
            logger.debug("← %s %s", response.status_code, url)
            if response.status_code < 500 or attempt == attempts - 1:
                return response
        raise GoPaySDKError(
            f"[GoPaySDK] Network error ({type(last_error).__name__}).",
            error_code=ErrorCode.NETWORK_ERROR,
        ) from last_error

    def close(self) -> None:
        if self._owns_http:
            self._http.close()


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as error:
        raise GoPaySDKError(
            "[GoPaySDK] The API returned a response that is not valid JSON.",
            error_code=ErrorCode.NETWORK_ERROR,
        ) from error


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
