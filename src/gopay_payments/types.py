"""Request and response shapes of the Payments API v4, as ``TypedDict``\\ s.

Generated from the published spec, so the names are the spec's schema names without
hyphens (``Payment-Create-Request`` → :class:`PaymentCreateRequest`). They exist for type
checkers and editors: at runtime every value is a plain ``dict``.
"""

from . import _generated
from ._generated import *  # noqa: F403

__all__ = [name for name in vars(_generated) if name[:1].isupper()]
