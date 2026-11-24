"""GoPay Payments API v4 — Python server-side SDK.

>>> from gopay_payments import GoPayClient, GoPayScopes, combine_scopes
>>> client = GoPayClient(environment="sandbox")
>>> client.authenticate(client_id, client_secret,
...     combine_scopes(GoPayScopes.PAYMENT_WRITE, GoPayScopes.PAYMENT_READ))

Request and response shapes are in :mod:`gopay_payments.types`.
"""

from ._events import GoPayEvent, UnknownEvent, is_known_event
from ._version import __version__
from .client import BrowserKeys, GoPayClient
from .environment import Environment
from .errors import ErrorCode, GoPayError, GoPayHTTPError, GoPaySDKError
from .scopes import GoPayScopes, combine_scopes
from .token_cache import CachedToken, InMemoryTokenCache, TokenCache

__all__ = [
    "BrowserKeys",
    "CachedToken",
    "Environment",
    "ErrorCode",
    "GoPayClient",
    "GoPayError",
    "GoPayEvent",
    "GoPayHTTPError",
    "GoPaySDKError",
    "GoPayScopes",
    "InMemoryTokenCache",
    "TokenCache",
    "UnknownEvent",
    "__version__",
    "combine_scopes",
    "is_known_event",
]
