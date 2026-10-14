"""Create a payment and send the customer to GoPay's hosted gateway.

uv run python examples/create_payment.py
"""

from _common import env, order, sandbox_client

with sandbox_client() as client:
    payment = client.create_payment(env("GOPAY_PAYMENTS_V4_GOID"), order())
    print("payment", payment["id"], payment["state"])
    print("pay at", payment["gw_url"])

    # When the customer returns (or GoPay calls notification_url), read the payment again.
    print("state now", client.get_payment_status(payment["id"])["state"])
