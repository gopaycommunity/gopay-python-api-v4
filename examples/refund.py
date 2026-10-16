"""Refund part of a paid payment and wait for the refund to settle.

uv run python examples/refund.py 3308747598    # the id of a PAID sandbox payment
"""

import sys

from _common import sandbox_client

if len(sys.argv) != 2:
    sys.exit("Usage: uv run python examples/refund.py PAYMENT_ID  (a PAID sandbox payment)")
payment_id = sys.argv[1]

with sandbox_client() as client:
    refund = client.refund_payment(payment_id, {"amount": 50})
    print("refund", refund["id"], refund["state"])  # REQUESTED: accepted, not settled

    settled = client.await_refund_state(refund["id"], timeout=120)
    print("refund", settled["id"], settled["state"])  # SUCCESS or FAILED — both are returned
    print("all refunds", [r["state"] for r in client.list_refunds(payment_id)])
