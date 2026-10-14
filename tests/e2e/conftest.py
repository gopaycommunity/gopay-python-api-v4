"""Live tests against a GoPay environment.

Run with ``uv run pytest -m e2e``. They read:

- ``GOPAY_PAYMENTS_V4_CLIENT_ID``, ``GOPAY_PAYMENTS_V4_CLIENT_SECRET``, ``GOPAY_PAYMENTS_V4_GOID``
- ``GOPAY_PAYMENTS_V4_ENVIRONMENT`` (default ``sandbox``) and ``GOPAY_PAYMENTS_V4_BASE_URL``
  (optional, for a non-default gateway)

They create and cancel real payments, so they refuse to run against production.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest

from gopay_payments import GoPayClient, GoPayScopes, combine_scopes
from gopay_payments.types import PaymentCreateRequest

PRODUCTION_HOSTS = {"gate.gopay.com"}


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        # Asking for the e2e suite without credentials is a mistake to report, not a
        # reason to pass silently.
        pytest.fail(f"{name} is not set — see tests/e2e/conftest.py", pytrace=False)
    return value


@pytest.fixture(scope="session")
def goid() -> str:
    return _require("GOPAY_PAYMENTS_V4_GOID")


@pytest.fixture(scope="session")
def client() -> Iterator[GoPayClient]:
    environment = os.environ.get("GOPAY_PAYMENTS_V4_ENVIRONMENT", "sandbox")
    base_url = os.environ.get("GOPAY_PAYMENTS_V4_BASE_URL") or None
    host = urlsplit(base_url).hostname if base_url else None
    if environment == "production" or host in PRODUCTION_HOSTS:
        pytest.fail("e2e tests create payments and must not run against production", pytrace=False)
    with GoPayClient(
        environment=environment,  # type: ignore[arg-type]
        base_url=base_url,
        user_agent_suffix="gopay-python-api-v4-e2e",
    ) as c:
        c.authenticate(
            _require("GOPAY_PAYMENTS_V4_CLIENT_ID"),
            _require("GOPAY_PAYMENTS_V4_CLIENT_SECRET"),
            combine_scopes(GoPayScopes.PAYMENT_WRITE, GoPayScopes.PAYMENT_READ),
        )
        yield c


@pytest.fixture
def payment_request() -> PaymentCreateRequest:
    return {
        "amount": 100,
        "currency": "CZK",
        "order_number": f"py-e2e-{uuid.uuid4().hex[:10]}",
        "order_description": "gopay-python-api-v4 e2e",
        "customer": {"email": "e2e@example.com"},
        "callback": {
            "return_url": "https://shop.example/return",
            "notification_url": "https://shop.example/notify",
        },
    }
