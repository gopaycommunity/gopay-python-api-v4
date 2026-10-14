"""Shared setup for the examples: a sandbox client from GOPAY_PAYMENTS_V4_* variables."""

from __future__ import annotations

import os
import sys
import uuid

from gopay_payments import GoPayClient, GoPayScopes, combine_scopes
from gopay_payments.types import PaymentCreateRequest


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Set {name} (see .env.example).")
    return value


def sandbox_client() -> GoPayClient:
    client = GoPayClient(environment="sandbox", user_agent_suffix="gopay-python-api-v4-examples")
    client.authenticate(
        env("GOPAY_PAYMENTS_V4_CLIENT_ID"),
        env("GOPAY_PAYMENTS_V4_CLIENT_SECRET"),
        combine_scopes(GoPayScopes.PAYMENT_WRITE, GoPayScopes.PAYMENT_READ),
    )
    return client


def order(amount: int = 100) -> PaymentCreateRequest:
    return {
        "amount": amount,
        "currency": "CZK",
        "order_number": f"example-{uuid.uuid4().hex[:8]}",
        "order_description": "gopay-python-api-v4 example",
        "customer": {"email": "buyer@example.com"},
        "callback": {
            "return_url": "https://shop.example/gopay/return",
            "notification_url": "https://shop.example/gopay/notify",
        },
    }
