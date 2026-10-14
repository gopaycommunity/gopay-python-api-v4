"""Create an on-demand recurrence, start it, and charge the next payment once it runs.

uv run python examples/recurrence.py
"""

from _common import env, order, sandbox_client

with sandbox_client() as client:
    rec = client.create_recurrence(
        env("GOPAY_PAYMENTS_V4_GOID"),
        {"type": "ON_DEMAND", "recurrence_date_to": "2030-12-31", "payment": order()},
    )
    first = client.start_recurrence(rec["id"])
    print("customer pays the first payment at", first["gw_url"])

    # STARTED once the customer has paid; raises if the first payment is canceled meanwhile.
    started = client.await_recurrence_state(rec["id"], timeout=600)
    if started["state"] == "STARTED":
        nxt = client.create_next_payment(rec["id"], {"amount": 100})
        print("next payment", nxt["id"], nxt["state"])
