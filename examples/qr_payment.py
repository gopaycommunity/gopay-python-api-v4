"""Show a QR payment and wait until it is paid, then cancel it if nobody pays in time.

uv run python examples/qr_payment.py
"""

from _common import env, order, sandbox_client

from gopay_payments import ErrorCode, GoPaySDKError

with sandbox_client() as client:
    payment = client.create_payment(env("GOPAY_PAYMENTS_V4_GOID"), order())
    qr = client.get_qr_payment_info(payment["id"])
    account = qr["recipient"]["bank_account"]["local"]
    print("pay", qr["amount"] / 100, qr["currency"], "to", account)

    try:
        paid = client.await_payment_status(payment["id"], timeout=60)
        print("final state", paid["state"])
    except GoPaySDKError as error:
        if error.error_code is not ErrorCode.CHARGE_TIMEOUT:
            raise
        client.cancel_payment(payment["id"])
        print("not paid within a minute — canceled")
