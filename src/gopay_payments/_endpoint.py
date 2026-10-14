from __future__ import annotations

import re

_ALL_DIGITS = re.compile(r"^\d+$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def normalize_endpoint(path: str) -> str:
    """Collapse a request path into a stable template for monitoring.

    Error trackers group by the fields they are handed, so a path carrying a payment id
    opens one group per payment. Only all-digit and UUID segments are rewritten: every
    literal segment of the v4 API is a word, so neither rule can swallow one. Same rule as
    the JavaScript SDK, so the two group identically.
    """
    without_query = path.split("?", 1)[0].lstrip("/")
    segments = [
        "{id}" if _ALL_DIGITS.match(s) or _UUID.match(s) else s for s in without_query.split("/")
    ]
    return "/" + "/".join(segments)
