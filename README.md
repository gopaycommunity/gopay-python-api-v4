# gopay-payments-v4

Python server-side SDK for the [GoPay Payments API v4](https://api-docs.gopay.com/).

- Every server operation of the API: payments, charges, refunds, payment links, recurrences
  and stored cards.
- Pollers that follow a charge, payment, refund or recurrence until it settles — including
  handing you the 3-D Secure redirect.
- Typed request and response shapes (`TypedDict`), generated from the published spec.
- Synchronous, thread-safe, one runtime dependency besides `typing_extensions`
  ([httpx](https://www.python-httpx.org/)).
- Python 3.9 and newer.

Moving from the v3 SDK (`gopay`)? See [MIGRATION.md](MIGRATION.md). The two packages can be
installed side by side.

## Installation

```bash
pip install gopay-payments-v4
```

## Quick start

```python
from gopay_payments import GoPayClient, GoPayScopes, combine_scopes

client = GoPayClient(environment="sandbox")  # "production" when you go live
client.authenticate(
    client_id,
    client_secret,
    combine_scopes(GoPayScopes.PAYMENT_WRITE, GoPayScopes.PAYMENT_READ),
)

payment = client.create_payment(goid, {
    "amount": 12900,             # in the smallest currency unit: 129.00 CZK
    "currency": "CZK",
    "order_number": "2026-0042",
    "customer": {"email": "buyer@example.com"},
    "callback": {
        "return_url": "https://shop.example/gopay/return",
        "notification_url": "https://shop.example/gopay/notify",
    },
})

print(payment["id"], payment["state"])  # "3308747598" "CREATED"
```

Create one client per set of credentials and reuse it — it keeps the access token and a
connection pool, and it is safe to share between threads. Expired tokens are replaced on
their own; you call `authenticate()` once, not before every request.

## Configuration

```python
GoPayClient(
    environment="sandbox",        # or "production"
    base_url=None,                # override the API URL (a mock server in tests)
    timeout=10.0,                 # per-request timeout, seconds
    user_agent_suffix=None,       # "MyShop/2.1" — appended to the SDK's own User-Agent
    shareable_key=None,           # returned by get_browser_keys()
    on_error=None,                # called once for every error the SDK raises
    token_cache=None,             # where to keep the access token
    http_client=None,             # an httpx.Client: proxies, custom TLS, test transports
)
```

| Environment | API base URL |
|---|---|
| `sandbox` | `https://gw.sandbox.gopay.com/gp-gw/api/4.0` |
| `production` | `https://gate.gopay.com/gp-gw/api/4.0` |

A `base_url` override must use HTTPS. Plain HTTP is accepted only for a local mock server in
the sandbox environment.

The SDK identifies itself with `User-Agent: GoPay Python v4 <version>`. A
`user_agent_suffix` is added after it, never in its place.

The client can be used as a context manager; `close()` releases the connection pool. A
`http_client` you pass in is never closed by the SDK.

### Scopes

| Scope | Grants |
|---|---|
| `payment:write` | create, charge, cancel and refund payments; manage payment links and recurrences |
| `payment:read` | read payments, charges, refunds, payment links and recurrences |
| `card:write` | tokenize encrypted cards, delete stored cards |
| `card:read` | read stored cards |

## Charging a card

The card never reaches your server in the clear. The GoPay-hosted card form in the browser
(the [browser SDK](https://www.npmjs.com/package/@gopaycz/gopay-js-sdk-browser)) encrypts it;
your server turns the encrypted payload into a token and charges the payment.

```python
# Values your checkout page sent: the encrypted card from the card form and the
# browser data from the browser SDK's getBrowserData().
card = client.tokenize_encrypted_card(encrypted_payload)   # needs scope card:write

client.charge_payment(payment_id, {
    "payment_instrument": {
        "payment_instrument": "PAYMENT_CARD",
        "input": {"input_type": "CARD_TOKEN", "card_token": card["token"]},
        "browser_data": browser_data,
    },
})

state = client.await_charge_state(
    payment_id,
    on_action_required=send_customer_to,   # receives the 3-D Secure redirect URL
)
```

**The response of `charge_payment()` is not the outcome.** A charge is processed
asynchronously: `await_charge_state()` follows it until it is `SUCCEEDED` (returned) or
`FAILED` (raised as `GoPaySDKError` with `error_code == ErrorCode.CHARGE_FAILED`; the final
state, `fail_reason` included, is on `error.charge_state`).

**When the bank asks for 3-D Secure, redirect the customer immediately.** The charge moves to
`ACTION_REQUIRED` and `on_action_required` receives the URL once. The bank keeps the
authentication open only briefly: queueing the redirect, e-mailing it or showing it as a link
for later finds the payment already abandoned. From that point the poller waits as long as the
customer needs.

**`browser_data` must come from the customer's browser.** Its `ip`, `user_agent` and
`accept_header` describe the connection the 3-D Secure challenge runs on. Fetch them in the
browser with the browser SDK and pass them through unchanged — never fill them in from the
request your server received. The address would be your proxy's or CDN's, and a charge run from
a queue has no customer request at all. Substituted values look valid and then fail
authentication.

To confirm the result independently of the charge — after the customer returns from 3-D
Secure, for example — read the payment with `get_payment_status()`.

## Hosted gateway, QR and bank transfer

Every created payment carries `gw_url`, the address of GoPay's hosted payment gateway. It
offers the customer every payment method the eshop has enabled and is a supported integration
of its own. Drive either the gateway or `charge_payment()` for a given payment, never both.

QR and bank-transfer payments are not charged. Show the customer the payment details and wait
for the payment to be paid:

```python
qr = client.get_qr_payment_info(payment_id)             # or format="svg"
show(qr["qr_code"], qr["recipient"])

final = client.await_payment_status(payment_id)         # PAID, CANCELED, TIMEOUTED, …
```

`await_payment_status()` has no timeout of its own — the API ends an unpaid payment on its own
schedule. Pass `timeout=` (seconds) for a client-side ceiling.

## API

### Authentication

| Method | |
|---|---|
| `authenticate(client_id, client_secret, scope)` | Authenticate. The token is never returned. |
| `is_authenticated()` | Whether a token is held. |
| `logout()` | Forget the credentials and the token. |
| `set_shareable_key(key)` | Set the key returned by `get_browser_keys()`. |
| `get_browser_keys()` | `{"shareable_key", "client_id"}` — what the browser SDK starts with. Both are safe to send to the browser. |

### Payments

| Method | Endpoint |
|---|---|
| `create_payment(goid, params)` | `POST /eshops/{goid}/payments` |
| `get_payment_status(payment_id)` | `GET /payments/{payment_id}` |
| `cancel_payment(payment_id)` | `DELETE /payments/{payment_id}` — only in `CREATED`; otherwise `409` |
| `charge_payment(payment_id, params)` | `POST /payments/{payment_id}/charge` |
| `get_charge_state(payment_id)` | `GET /payments/{payment_id}/charge` |
| `get_qr_payment_info(payment_id, format=None)` | `GET /payments/{payment_id}/qr-payment/info` |
| `get_google_pay_info(payment_id)` | `GET /payments/{payment_id}/google-pay/info` |
| `get_apple_pay_info(payment_id)` | `GET /payments/{payment_id}/apple-pay/info` |
| `validate_apple_pay_merchant(payment_id, params, origin=...)` | `POST /payments/{payment_id}/apple-pay/validate` |
| `await_charge_state(payment_id, ...)` | polls the charge until it settles |
| `await_payment_status(payment_id, ...)` | polls the payment until a terminal state |

`goid` may be a string or an int.

### Refunds

| Method | Endpoint |
|---|---|
| `refund_payment(payment_id, {"amount": ...})` | `POST /payments/{payment_id}/refunds` |
| `list_refunds(payment_id)` | `GET /payments/{payment_id}/refunds` |
| `get_refund(refund_id)` | `GET /refunds/{refund_id}` |
| `await_refund_state(refund_id, ...)` | polls until `SUCCESS` or `FAILED` |

A refund comes back `REQUESTED` — accepted, not settled. `await_refund_state()` returns
`FAILED` rather than raising it: it is an outcome to inspect.

### Payment links

| Method | Endpoint |
|---|---|
| `create_payment_link(goid, params)` | `POST /eshops/{goid}/links` |
| `get_payment_link(goid, link_id)` | `GET /eshops/{goid}/links/{link_id}` |
| `disable_payment_link(goid, link_id)` | `DELETE /eshops/{goid}/links/{link_id}` |

A link stores payment data; no payment exists until a customer opens it. Its `id` and `url`
cannot be derived from each other — keep `id` if you want to manage the link later.
Disabling keeps the link readable with `stop_reason: "FROM_API"`.

### Recurrences

| Method | Endpoint |
|---|---|
| `create_recurrence(goid, params)` | `POST /eshops/{goid}/recurrences` |
| `get_recurrence(rec_id)` | `GET /recurrences/{rec_id}` |
| `start_recurrence(rec_id, override=None)` | `POST /recurrences/{rec_id}/start` |
| `create_next_payment(rec_id, override=None)` | `POST /recurrences/{rec_id}/next` |
| `stop_recurrence(rec_id)` | `DELETE /recurrences/{rec_id}` |
| `await_recurrence_state(rec_id, ...)` | polls until `STARTED` or `STOPPED` |

Creating a recurrence charges nothing. `start_recurrence()` creates the first payment, which
the customer pays at its `gw_url` — for recurrences the hosted gateway is the intended flow.
Only then is the recurrence `STARTED` and accepts `create_next_payment()`.

`await_recurrence_state()` raises instead of waiting forever when the recurrence can no longer
start: still `NEW` (`INVALID_ARGUMENT` — call `start_recurrence()`), or its first payment was
canceled or timed out (`CHARGE_FAILED`).

A recurrence waiting for its first payment cannot be stopped (`409`); cancel that payment first.

### Stored cards

| Method | Endpoint |
|---|---|
| `tokenize_encrypted_card(payload)` | `POST /cards/tokens` (`card:write`) |
| `get_card_details(card_id)` | `GET /cards/tokens/{card_id}` (`card:read`) |
| `delete_card(card_id)` | `DELETE /cards/tokens/{card_id}` (`card:write`) |

### Polling options

All pollers take `on_state_change` (called with every polled state), `interval` (seconds,
at least 1), and `cancel` — a `threading.Event` that stops polling when set. A poller raising
`CHARGE_TIMEOUT` or a cancelled poller raising `CHARGE_FAILED` has not changed anything on the
API side; read the resource again to see where it stands.

| Poller | Default interval | Timeout |
|---|---|---|
| `await_charge_state` | 2 s | `initial_timeout=30` — lifted once 3-D Secure starts |
| `await_payment_status` | 3 s | none unless `timeout=` |
| `await_refund_state` | 3 s | none unless `timeout=` |
| `await_recurrence_state` | 3 s | none unless `timeout=` |

The pollers block the calling thread. In a web request, prefer reacting to GoPay's
notification (`callback.notification_url`) and reading the payment with
`get_payment_status()` over blocking a worker while the customer pays.

## Errors

Every error the SDK raises is a `GoPayError`:

- **`GoPayHTTPError`** — the API answered with a non-2xx status. `status`, `body` (parsed JSON,
  or text), `method`, and `endpoint` (the path with ids collapsed: `/payments/{id}/charge`).
- **`GoPaySDKError`** — the SDK gave up. `error_code` is an `ErrorCode`:

| `ErrorCode` | |
|---|---|
| `AUTH_TOKEN_MISSING` | no token — call `authenticate()` |
| `AUTH_REFRESH_FAILED` | replacing an expired token failed; call `authenticate()` again |
| `AUTH_INVALID_RESPONSE` | the token response was incomplete |
| `AUTH_CREDENTIALS_MISSING` | `get_browser_keys()` without a shareable key or authentication |
| `AUTH_UNAUTHORIZED` | a fresh token was rejected — usually a missing scope |
| `NETWORK_TIMEOUT` | the request timed out |
| `NETWORK_ERROR` | no usable response arrived |
| `CHARGE_TIMEOUT` | a poller's timeout elapsed |
| `CHARGE_FAILED` | the charge failed, a recurrence can no longer start, or polling was cancelled |
| `INVALID_CONFIG` | invalid client configuration |
| `INVALID_ARGUMENT` | a missing or invalid argument |

```python
from gopay_payments import ErrorCode, GoPayHTTPError, GoPaySDKError

try:
    client.cancel_payment(payment_id)
except GoPayHTTPError as error:
    if error.status == 409:
        ...  # the payment is no longer CREATED
```

Requests that are safe to repeat (`GET`, `DELETE`) are retried twice on a `5xx` or a dropped
connection. A `POST` is never retried: a charge or refund that reached the API must not be sent
twice.

## Monitoring

`on_error` is the place to plug in your own monitoring. It is called once for every error,
before it propagates; it cannot suppress the error, and anything it raises is logged and
ignored.

```python
import sentry_sdk
from gopay_payments import GoPayClient, GoPayHTTPError

def report(error):
    with sentry_sdk.new_scope() as scope:
        if isinstance(error, GoPayHTTPError):
            scope.set_tag("gopay_status", error.status)
            scope.set_tag("gopay_endpoint", error.endpoint)
        else:
            scope.set_tag("gopay_code", error.error_code)
        sentry_sdk.capture_exception(error)

client = GoPayClient(environment="production", on_error=report)
```

Tag `error_code`, or `status` with `endpoint` — they are what make events groupable. Do not
attach the error `body` verbatim: it is the API's response and may name the customer. Never
forward the arguments you passed in either — a charge carries a card token.

Debug logs (method, URL, status — never headers or bodies) go to the `gopay_payments` logger.

## Running many processes

By default each client keeps its token in memory, so every process obtains its own. When many
processes share the same credentials — several workers, short-lived jobs — give them a shared
`TokenCache` so they reuse one token:

```python
from gopay_payments import CachedToken, GoPayClient

class RedisTokenCache:
    def __init__(self, redis):
        self.redis = redis

    def get(self, key):
        raw = self.redis.hgetall(key)
        if not raw:
            return None
        return CachedToken(raw[b"token"].decode(), float(raw[b"expires_at"]))

    def set(self, key, token):
        self.redis.hset(key, mapping={"token": token.access_token, "expires_at": token.expires_at})
        self.redis.expireat(key, int(token.expires_at))

    def delete(self, key):
        self.redis.delete(key)

client = GoPayClient(environment="production", token_cache=RedisTokenCache(redis))
```

The cache holds a bearer token: store it as you would any other credential. Its key never
contains the client secret.

## Types

Request and response shapes are `TypedDict`s in `gopay_payments.types`, named after the spec's
schemas (`Payment-Create-Request` → `PaymentCreateRequest`). They help type checkers and
editors; at runtime every value is a plain `dict`.

```python
from gopay_payments.types import PaymentCreateRequest

order: PaymentCreateRequest = {...}
```

## Examples

[`examples/`](examples/) has runnable scripts for a payment, a QR payment, a refund and a
recurrence against the sandbox.

## License

MIT — see [LICENSE](LICENSE).
