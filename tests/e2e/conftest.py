"""Live tests against a GoPay environment.

Run with ``uv run pytest -m e2e``. They read:

- ``GOPAY_PAYMENTS_V4_CLIENT_ID``, ``GOPAY_PAYMENTS_V4_CLIENT_SECRET``, ``GOPAY_PAYMENTS_V4_GOID``
- ``GOPAY_PAYMENTS_V4_BASE_URL`` (optional): a gateway other than the public sandbox
- ``GOPAY_E2E_WEBHOOK_INSPECTION_URL`` (optional): demo-site's webhook receiver on the same
  environment, which the webhook delivery test reads back from; skipped without it

The events tests ask for the ``event:read`` and ``shared:read`` scopes too.

They create and cancel real payments, so they run on the sandbox environment only and refuse a
base URL that points at production.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Callable
from urllib.parse import urlsplit

import pytest

from gopay_payments import Environment, GoPayClient, GoPayScopes, combine_scopes
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


def _client(*scopes: str) -> Iterator[GoPayClient]:
    base_url = os.environ.get("GOPAY_PAYMENTS_V4_BASE_URL") or None
    host = urlsplit(base_url).hostname if base_url else None
    if host in PRODUCTION_HOSTS:
        pytest.fail("e2e tests create payments and must not run against production", pytrace=False)
    with GoPayClient(
        environment=Environment.SANDBOX,
        base_url=base_url,
        user_agent_suffix="gopay-python-api-v4-e2e",
    ) as c:
        c.authenticate(
            _require("GOPAY_PAYMENTS_V4_CLIENT_ID"),
            _require("GOPAY_PAYMENTS_V4_CLIENT_SECRET"),
            combine_scopes(*scopes),
        )
        yield c


@pytest.fixture(scope="session")
def client() -> Iterator[GoPayClient]:
    yield from _client(GoPayScopes.PAYMENT_WRITE, GoPayScopes.PAYMENT_READ)


@pytest.fixture(scope="session")
def events_client() -> Iterator[GoPayClient]:
    """A client that may also read events and the webhook keys.

    A fixture of its own: credentials without event:read or shared:read fail these tests
    alone, not every live test.
    """
    yield from _client(
        GoPayScopes.PAYMENT_WRITE,
        GoPayScopes.PAYMENT_READ,
        GoPayScopes.EVENT_READ,
        GoPayScopes.SHARED_READ,
    )


def _payment_request() -> PaymentCreateRequest:
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


@pytest.fixture
def payment_request() -> PaymentCreateRequest:
    return _payment_request()


@pytest.fixture(scope="session")
def payment_request_factory() -> Callable[[], PaymentCreateRequest]:
    """A new payment request per call, for fixtures wider than one test."""
    return _payment_request
