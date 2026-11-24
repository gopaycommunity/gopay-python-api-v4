"""OAuth2 scopes for :meth:`GoPayClient.authenticate`."""

from __future__ import annotations


class GoPayScopes:
    """Scope strings the server SDK can request.

    Combine several with :func:`combine_scopes`.
    """

    PAYMENT_WRITE = "payment:write"
    """Create, charge, cancel and refund payments; manage payment links and recurrences."""
    PAYMENT_READ = "payment:read"
    """Read payments, charges, refunds, payment links and recurrences."""
    CARD_WRITE = "card:write"
    """Tokenize encrypted cards and delete stored card tokens."""
    CARD_READ = "card:read"
    """Read stored card tokens."""
    EVENT_READ = "event:read"
    """Read the eshop's events: :meth:`GoPayClient.list_events`, ``get_event``, ``iter_events``."""
    SHARED_READ = "shared:read"
    """Read public resources such as the webhook signing keys :meth:`GoPayClient.verify_webhook`
    needs."""


def combine_scopes(*scopes: str) -> str:
    """Join scopes into the space-separated string the API expects."""
    return " ".join(scopes)
