from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlsplit

from .errors import ErrorCode, GoPaySDKError


def require_non_empty_string(value: Any, field: str) -> str:
    """Return ``value`` stripped, or raise ``INVALID_ARGUMENT`` if it is blank."""
    if not isinstance(value, str) or not value.strip():
        raise GoPaySDKError(
            f"[GoPaySDK] {field} is required", error_code=ErrorCode.INVALID_ARGUMENT
        )
    return value.strip()


def require_path_segment(value: Any, field: str) -> str:
    """Validate ``value`` as one URL path segment and return it percent-encoded.

    Ids are interpolated into request paths. An unencoded ``../recurrences/123`` would
    reach a different endpoint than the caller named, so the traversal segments are
    refused and everything outside RFC 3986's unreserved set is encoded. ``quote`` with
    ``safe=""`` encodes exactly that set, which is also what the PHP SDK's
    ``rawurlencode`` and the JavaScript SDK put on the wire.
    """
    validated = require_non_empty_string(value, field)
    if validated in (".", ".."):
        raise GoPaySDKError(
            f'[GoPaySDK] {field} must not be "." or ".."',
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
    return quote(validated, safe="")


def require_https_origin(origin: Any, field: str) -> str:
    """Return ``origin`` if it is a bare ``https://host[:port]`` origin."""
    if not isinstance(origin, str):
        raise GoPaySDKError(
            f"[GoPaySDK] {field} must be a string", error_code=ErrorCode.INVALID_ARGUMENT
        )
    parts = urlsplit(origin)
    if (
        parts.scheme != "https"
        or not parts.netloc
        or parts.path
        or parts.query
        or parts.fragment
        or "@" in parts.netloc
    ):
        raise GoPaySDKError(
            f'[GoPaySDK] {field} must be an https origin such as "https://shop.example". '
            f'Got "{origin}"',
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
    return origin


_HEADER_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")


def require_header_value(value: Any, field: str) -> str:
    """Return ``value`` if it can go into an HTTP header verbatim."""
    if not isinstance(value, str) or not value.strip() or _HEADER_UNSAFE.search(value):
        raise GoPaySDKError(
            f"[GoPaySDK] {field} must be a non-empty string without control characters",
            error_code=ErrorCode.INVALID_CONFIG,
        )
    return value.strip()
