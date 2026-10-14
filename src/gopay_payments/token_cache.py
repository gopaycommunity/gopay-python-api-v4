"""Where the client keeps its access token.

By default a token lives in the client's memory, so every new process obtains its own.
Where many processes serve the same eshop — several workers, short-lived jobs — pass a
:class:`TokenCache` backed by shared storage (Redis, the framework's cache) so they reuse
one token instead of each requesting a new one.

The cached value is a bearer token. Store it where you would store any other credential.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from typing_extensions import Protocol


@dataclass(frozen=True)
class CachedToken:
    """An access token and the moment it stops being valid."""

    access_token: str
    expires_at: float
    """Unix timestamp, in seconds."""


class TokenCache(Protocol):
    """Storage for access tokens, keyed by a string the SDK derives.

    The key identifies the environment, client id and scope. It never contains the client
    secret. Implementations only need to be as consistent as the storage they wrap: the
    client re-checks expiry itself and treats a missing entry as "obtain a new token".
    """

    def get(self, key: str) -> Optional[CachedToken]: ...

    def set(self, key: str, token: CachedToken) -> None: ...

    def delete(self, key: str) -> None: ...


class InMemoryTokenCache:
    """The default cache: a dictionary private to the client, safe across threads."""

    def __init__(self) -> None:
        self._tokens: dict[str, CachedToken] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[CachedToken]:
        with self._lock:
            return self._tokens.get(key)

    def set(self, key: str, token: CachedToken) -> None:
        with self._lock:
            self._tokens[key] = token

    def delete(self, key: str) -> None:
        with self._lock:
            self._tokens.pop(key, None)
