# Migrating from `gopay` (Payments API v3)

`gopay-payments-v4` is a new package for a new API. It is not source compatible with the v3
SDK, `gopay` on PyPI, and does not replace it in place: the two have different distribution
and import names (`gopay` vs. `gopay_payments`), so both can be installed while you move one
flow at a time. The v3 SDK keeps working against the v3 API.

Runtime requirements are the same: Python 3.9 or newer.

## What changes

### One client, explicit authentication

```python
# v3
import gopay
api = gopay.payments({
    "goid": GOID, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
    "gateway_url": "https://gw.sandbox.gopay.com/api", "scope": "payment-all",
})

# v4
from gopay_payments import GoPayClient, GoPayScopes, combine_scopes
client = GoPayClient(environment="sandbox")
client.authenticate(CLIENT_ID, CLIENT_SECRET,
                    combine_scopes(GoPayScopes.PAYMENT_WRITE, GoPayScopes.PAYMENT_READ))
```

- **`gateway_url` becomes `environment`** (`"sandbox"` / `"production"`). You no longer
  build the URL yourself; `base_url` exists only for mock servers.
- **`goid` moves from the configuration to the calls that need it** (`create_payment(goid, …)`).
- **Scopes are new**: `payment:write`, `payment:read`, `card:write`, `card:read` replace
  `payment-create` / `payment-all`.

### Errors are raised, not returned

```python
# v3
response = api.get_status(payment_id)
if response.success:
    state = response.json["state"]

# v4
try:
    state = client.get_payment_status(payment_id)["state"]
except GoPayHTTPError as error:     # the API answered with a non-2xx status
    ...
except GoPaySDKError as error:      # timeout, network, validation, …
    ...
```

Methods return the parsed JSON body (a `dict`) directly.

### Payments: your own checkout — or the hosted gateway

In v3 you created a payment and sent the customer to `gw_url`. In v4 you choose per payment:

- **Your own checkout**: the server creates the payment and hands its `id` and
  `payment_secret` to GoPay's client SDK (browser, iOS, Android), which takes the card, a
  saved card or a wallet and charges the payment, 3-D Secure included. The server then
  confirms the outcome with `get_charge_state()` and from the webhook (`verify_webhook()`).
  Charging from the server with `charge_payment()` remains possible when it has to act on the
  card first. See the README.
- **The hosted gateway**: the created payment still carries `gw_url`, GoPay's full payment
  gateway with every method the eshop has enabled. It is a supported v4 integration, not a v3
  leftover.

### Notifications become signed webhooks

v3 called `notification_url` with the payment id and left you to read the payment. v4 sends a
signed webhook carrying an event (`payments.paid`, `refunds.succeeded`, …): check it with
`verify_webhook()`, then read the current state. The same events can be listed back for 30
days with `iter_events()`.

QR and bank-transfer payments are not charged: show `get_qr_payment_info()` and wait with
`await_payment_status()`.

### Refunds and recurrences are resources of their own

- A refund has its own id and state. `refund_payment()` returns it as `REQUESTED`;
  `await_refund_state()` waits until it settles.
- A recurrence is created against the eshop, not on top of an existing payment:
  `create_recurrence(goid, …)` stores the template, `start_recurrence()` creates the first
  payment, `create_next_payment()` the following ones.

### Smaller differences

| | v3 | v4 |
|---|---|---|
| Token cache | `AbstractCache` subclass in `services` | any object with `get` / `set` / `delete` (`TokenCache`) |
| Timeout | `timeout` config, default 3600 s | `timeout=`, default 10 s, for each phase of a request |
| User-Agent | `custom_user_agent` replaces the SDK's | `user_agent_suffix` is appended to `GoPay Python v4 <version>` |
| Logging | custom logger in `services` | the standard `gopay_payments` logger |
| Request bodies | the dict you pass may be modified | the dict you pass is left alone |

## Method by method

| v3 (`gopay`) | v4 (`gopay_payments`) |
|---|---|
| `create_payment(payment)` | `create_payment(goid, params)` |
| `get_status(payment_id)` | `get_payment_status(payment_id)` |
| `get_qr_payment(payment_id, …)` | `get_qr_payment_info(payment_id, format=None)` |
| `refund_payment(payment_id, amount)` | `refund_payment(payment_id, {"amount": amount})` |
| `get_history_of_refunds(payment_id)` | `list_refunds(payment_id)` |
| `create_recurrence(payment_id, payment)` | `create_recurrence(goid, params)` + `start_recurrence(rec_id)` / `create_next_payment(rec_id)` |
| `void_recurrence(payment_id)` | `stop_recurrence(rec_id)` |
| `get_card_details(card_id)` | `get_card_details(card_id)` |
| `delete_card(card_id)` | `delete_card(card_id)` |
| `capture_authorization(…)`, `capture_authorization_partial(…)`, `void_authorization(…)` | not in v4 yet |
| `get_payment_instruments(…)`, `get_payment_instruments_all(…)` | not in v4 |
| `get_account_statement(…)` | not in v4 |
| `refund_payment_eet(…)`, `get_eet_receipt_by_payment_id(…)`, `find_eet_receipts_by_filter(…)` | not in v4 |
| `get_embedjs_url()` | not in v4 — the browser SDK replaces the embedded gateway |

New in v4, with no v3 counterpart: `cancel_payment`, `charge_payment`, `get_charge_state`,
payment links, `tokenize_encrypted_card`, the `await_*` pollers, `verify_webhook`, the events
(`list_events`, `get_event`, `iter_events`) and `with_transaction_id`.
