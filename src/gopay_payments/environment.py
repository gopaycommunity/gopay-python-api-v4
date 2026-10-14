"""Target environments and their API base URLs."""

from __future__ import annotations

from enum import Enum
from typing import Optional, Union
from urllib.parse import urlsplit

from .errors import ErrorCode, GoPaySDKError


class Environment(str, Enum):
    """The GoPay environment a client talks to."""

    SANDBOX = "sandbox"
    PRODUCTION = "production"


BASE_URLS = {
    Environment.SANDBOX: "https://gw.sandbox.gopay.com/gp-gw/api/4.0",
    Environment.PRODUCTION: "https://gate.gopay.com/gp-gw/api/4.0",
}

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def parse_environment(value: Union[Environment, str]) -> Environment:
    try:
        return Environment(value)
    except ValueError:
        raise GoPaySDKError(
            f'[GoPaySDK] environment must be "sandbox" or "production". Got {value!r}',
            error_code=ErrorCode.INVALID_CONFIG,
        ) from None


def resolve_base_url(environment: Environment, base_url: Optional[str]) -> str:
    """Return the API base URL, validating an explicit override.

    An override must use HTTPS. Plain HTTP is accepted only for a local mock server in the
    sandbox environment, so a production client can never send credentials in the clear.
    """
    if base_url is None:
        return BASE_URLS[environment]

    parts = urlsplit(base_url) if isinstance(base_url, str) else None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        raise GoPaySDKError(
            f'[GoPaySDK] base_url is not a valid URL: "{base_url}"',
            error_code=ErrorCode.INVALID_CONFIG,
        )
    insecure_allowed = environment is Environment.SANDBOX and parts.hostname in _LOCAL_HOSTS
    if parts.scheme != "https" and not insecure_allowed:
        raise GoPaySDKError(
            f'[GoPaySDK] base_url must use HTTPS. Got "{base_url}". '
            "Plain HTTP is only permitted for localhost in the sandbox environment.",
            error_code=ErrorCode.INVALID_CONFIG,
        )
    return base_url.rstrip("/")
