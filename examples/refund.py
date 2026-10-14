"""Refund part of a paid payment and wait for the refund to settle.

PAYMENT_ID=3308747598 uv run python examples/refund.py
"""

from _common import env, sandbox_client

with sandbox_client() as client:
    payment_id = env("PAYMENT_ID")
    refund = client.refund_payment(payment_id, {"amount": 50})
    print("refund", refund["id"], refund["state"])  # REQUESTED: accepted, not settled

    settled = client.await_refund_state(refund["id"], timeout=120)
    print("refund", settled["id"], settled["state"])  # SUCCESS or FAILED — both are returned
    print("all refunds", [r["state"] for r in client.list_refunds(payment_id)])
