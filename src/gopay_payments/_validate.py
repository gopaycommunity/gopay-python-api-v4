from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

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


# Printable ASCII: what an HTTP header value can carry without httpx refusing to encode it.
_HEADER_SAFE = re.compile(r"[\x20-\x7e]+")


def require_header_value(value: Any, field: str) -> str:
    """Return ``value`` if it can go into an HTTP header verbatim."""
    if not isinstance(value, str) or not value.strip() or not _HEADER_SAFE.fullmatch(value):
        raise GoPaySDKError(
            f"[GoPaySDK] {field} must be a non-empty string of printable ASCII characters",
            error_code=ErrorCode.INVALID_CONFIG,
        )
    return value.strip()
