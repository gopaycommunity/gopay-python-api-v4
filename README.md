# gopay-payments-v4

[![PyPI Version](https://img.shields.io/pypi/v/gopay-payments-v4)](https://pypi.org/project/gopay-payments-v4/)
[![Python Version](https://img.shields.io/pypi/pyversions/gopay-payments-v4)](https://pypi.org/project/gopay-payments-v4/)
[![License](https://img.shields.io/pypi/l/gopay-payments-v4)](https://github.com/gopaycommunity/gopay-python-api-v4/blob/master/LICENSE)
[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=gp-gopay_gopay-python-api-v4&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=gp-gopay_gopay-python-api-v4)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=gp-gopay_gopay-python-api-v4&metric=coverage)](https://sonarcloud.io/summary/new_code?id=gp-gopay_gopay-python-api-v4)

Python server-side SDK for the [GoPay Payments API v4](https://api-docs.gopay.com/).

Your server creates the payment and confirms its outcome; GoPay's client SDK — the
[browser SDK](https://www.npmjs.com/package/@gopaycz/gopay-js-sdk-browser), or the iOS and
Android SDKs — takes the payment in your checkout: card form, wallets, 3-D Secure. Card data
never passes through your code.

- Every operation a merchant's server calls: payments, refunds, payment links, recurrences,
  saved cards, events and webhook verification. The operations GoPay's client SDKs call
  themselves — wallet configuration, Apple Pay merchant validation, the card form — are left out.
- Webhook verification against GoPay's signing keys, and an Events API iterator.
- Request ids on every call, and on every error, that GoPay support can find a call by.
- Pollers that follow a charge, payment, refund or recurrence until it settles — including
  handing you the 3-D Secure redirect.
- Typed request and response shapes (`TypedDict`), generated from the published spec.
- Synchronous only, and thread-safe. There is no `asyncio` client; from async code, run a
  call in a worker thread: `await asyncio.to_thread(client.get_payment_status, payment_id)`.
- One runtime dependency besides `typing_extensions` ([httpx](https://www.python-httpx.org/)),
  plus [cryptography](https://cryptography.io/) for webhooks.
- Python 3.9 and newer.

Moving from the v3 SDK (`gopay`)? See [MIGRATION.md](MIGRATION.md). The two packages can be
installed side by side.

## Installation

```bash
pip install gopay-payments-v4
pip install 'gopay-payments-v4[webhooks]'   # to verify webhooks
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

# 1. Your checkout endpoint creates the payment ...
payment = client.create_payment(goid, {
    "amount": 12900,             # in the smallest currency unit: 129.00 CZK
    "currency": "CZK",
    "order_number": "2026-0042",
    "customer": {"email": "buyer@example.com"},
    "callback": {"return_url": "https://shop.example/gopay/return?order=2026-0042"},
})

# 2. ... and hands id and payment_secret to the client SDK, which charges the payment.
response = {"id": payment["id"], "payment_secret": payment["payment_secret"]}

# 3. Back on return_url (and on the webhook), your server reads the outcome itself.
state = client.get_charge_state(payment["id"])
if state["state"] == "SUCCEEDED":
    ...  # the order is paid
```

Create one client per set of credentials and reuse it — it keeps the access token and a
connection pool, and it is safe to share between threads. Expired tokens are replaced on
their own; you call `authenticate()` once, not before every request.

## Configuration

```python
GoPayClient(
    environment="sandbox",        # or "production"
    base_url=None,                # override the API URL (a mock server in tests)
    timeout=10.0,                 # seconds for each phase of a request, see below
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

`timeout` limits each phase of an HTTP request — connecting, sending, waiting for the
response, reading it — not the whole call. A call that retries or polls makes several
requests and takes longer.

The SDK identifies itself with `User-Agent: GoPay Python v4 <version>`. A
`user_agent_suffix` is added after it, never in its place.

The client can be used as a context manager; `close()` releases the connection pool. A
`http_client` you pass in is never closed by the SDK.

### Scopes

| Scope | Grants |
|---|---|
| `payment:write` | create, charge, cancel and refund payments; manage payment links and recurrences |
| `payment:read` | read payments, charges, refunds, payment links and recurrences |
| `card:write` | save encrypted cards, delete saved cards |
| `card:read` | read saved cards |
| `event:read` | list and read the eshop's events |
| `shared:read` | read public resources such as the webhook signing keys `verify_webhook()` needs |

## Handing the payment to the client

The client SDK starts with two public keys, and charges a payment it is given:

```python
# Once, for the client SDK's initialisation. Both are safe to send to the client.
keys = client.get_browser_keys()   # {"shareable_key": ..., "client_id": ...}

# Per payment, from your own authenticated endpoint:
{"id": payment["id"], "payment_secret": payment["payment_secret"]}
```

- **`payment_secret` lets the client SDK act on the payment.** Send it only over your own
  authenticated channel, in the response that created the payment. Never put it in a URL —
  `return_url` included — a log, or storage. Keep the payment `id` with the order instead.
- **iOS and Android apps also get the `return_url`** the payment was created with: the SDK
  recognises the end of 3-D Secure by it. An app without a website creates the payment with
  `https://gopay.com/sdk/charge-return` as `return_url`.
- A payment is charged once. After a `FAILED` charge, a new attempt needs a new payment.

How the client SDK takes the card, saved card or wallet is in its own documentation.

## Confirming the result

The outcome the client SDK reports is for the customer's eyes. Your server confirms it with
its own token before it treats the order as paid.

**On the return page.** After 3-D Secure the customer's browser comes back to `return_url` —
a new page load. Resolve the payment from your own order reference, not from anything in the
URL, and read it once:

```python
state = client.get_charge_state(order.payment_id)
paid = state["state"] == "SUCCEEDED"   # FAILED: offer a new payment
```

A charge still `PENDING` or `PROCESSING` here settles shortly; the webhook tells you when.

**From the webhook.** GoPay delivers a `payments.*` event as a webhook — the authoritative
signal, which also covers a customer who never comes back. See [Webhooks](#webhooks).

## Webhooks

GoPay signs each webhook. `verify_webhook()` checks the signature against GoPay's published
keys and returns the event. It needs the `webhooks` extra and the `shared:read` scope.

```python
# Django
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from gopay_payments import ErrorCode, GoPaySDKError, is_known_event

@csrf_exempt
@require_POST
def gopay_webhook(request):
    try:
        event = client.verify_webhook(request.body, request.headers.get("X-GoPay-Signature"))
    except GoPaySDKError as error:
        # Not from GoPay: 400. Anything else: 500, and GoPay sends the webhook again.
        return HttpResponse(status=400 if error.error_code is ErrorCode.WEBHOOK_INVALID else 500)
    handle(event)
    return HttpResponse(status=204)
```

```python
# Flask
from flask import request

@app.post("/gopay/webhook")
def gopay_webhook():
    try:
        event = client.verify_webhook(request.get_data(), request.headers.get("X-GoPay-Signature"))
    except GoPaySDKError as error:
        return "", 400 if error.error_code is ErrorCode.WEBHOOK_INVALID else 500
    handle(event)
    return "", 204
```

**Pass the body exactly as received** — Django `request.body`, Flask `request.get_data()`.
The signature covers those bytes; a body parsed and serialised again no longer verifies, and
parsed JSON is refused with `INVALID_ARGUMENT`.

**Answer by the error code.** `WEBHOOK_INVALID` means the request is not a webhook GoPay
signed: answer `4xx`. Everything else — `WEBHOOK_KEYS_UNAVAILABLE` when the signing keys could
not be loaded, `INVALID_ARGUMENT`, `INVALID_CONFIG` without the extra — is your side: answer
`5xx`, so GoPay retries once it is fixed. Neither `WEBHOOK_*` code reaches `on_error`, since
anyone who can reach the endpoint can cause one; a failed key request does, once per attempt.

**Handle each event once.** GoPay may deliver an event more than once; deduplicate on its
`id`. Treat the event as a signal and read the current state:

```python
def handle(event):
    if not is_known_event(event):
        return                      # a type added after this SDK version: ignore it
    payment_id = event["data"].get("payment_id")
    if event["type"].startswith("payments.") and payment_id:
        payment = client.get_payment_status(payment_id)
        ...
```

An event of a type this SDK version does not know comes back as sent, never as an error.
`is_known_event()` tells the documented types apart.

The keys are fetched with the client's token, kept by `kid` for the life of the client and
refreshed when GoPay rotates them. `get_webhook_public_keys()` returns them as a JWK Set, for
verifying elsewhere.

## Events

The same events, kept for 30 days, can be read back with the `event:read` scope — to catch up
after an outage, or to see what happened to one payment:

```python
for event in client.iter_events(goid, {"related_type": "payments", "related_id": payment_id}):
    print(event["occurred_at"], event["type"])
```

`iter_events()` pages through every match, newest first, fetching the next page only when the
previous one is used up. `list_events()` returns one page, `get_event()` one event by `id`.
Filters: `type` (comma-separated, `"payments.*"` for a namespace), `related_type` with
`related_id`, `occurred_after` and `occurred_before` (an aware `datetime`, sent with its
milliseconds, or an RFC 3339 string), and `max_items`.

## Saved cards

Saving cards must be enabled for the eshop. The client SDK's card form returns the card
encrypted; your server exchanges that for a permanent token and hands the token back to the
client SDK, which charges with it.

```python
card = client.tokenize_encrypted_card(encrypted_payload)   # needs scope card:write
save_for_customer(card["card_id"], card["token"])
```

The encrypted payload is single-use: the one tokenised cannot also charge a payment. Read a
saved card with `get_card_details()` (`card:read`) and delete it with `delete_card()`
(`card:write`).

## Charging from the server

The client SDK charges by default. Your server charges instead only when it has to act on the
card before the charge. It then takes over two things the client SDK otherwise does:
collecting the browser data and handling 3-D Secure.

```python
# Both values come from your checkout page: the card form's encrypted card
# (flow "return-payload") and the client SDK's getBrowserData().
client.charge_payment(payment_id, {
    "payment_instrument": {
        "payment_instrument": "PAYMENT_CARD",
        "input": {"input_type": "ENCRYPTED_CARD", "payload": encrypted_payload},
        # or, for a saved card: {"input_type": "CARD_TOKEN", "card_token": card_token}
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
customer needs. The URL is handed out only while the customer still has to complete the
authentication, so a poller started after they are back never sends them to a finished one.
Alternatively, return from your endpoint and let the client SDK's `awaitChargeState()` take
the customer through the step.

**`browser_data` must come from the customer's browser.** Its `ip`, `user_agent` and
`accept_header` describe the connection the 3-D Secure challenge runs on. Fetch them in the
browser with the client SDK immediately before the charge and pass them through unchanged —
never fill them in from the request your server received. The address would be your proxy's
or CDN's, and a charge run from a queue has no customer request at all. Substituted values
look valid and then fail authentication.

The encrypted card is single-use and expires shortly: forward it without delay.

## Hosted gateway, QR and bank transfer

Every created payment carries `gw_url`, the address of GoPay's hosted payment gateway. It
offers the customer every payment method the eshop has enabled and is a supported integration
of its own. Drive either the gateway or a charge for a given payment, never both.

QR and bank-transfer payments are not charged. Show the customer the payment details and wait
for the payment to be paid:

```python
qr = client.get_qr_payment_info(payment_id)             # or format="svg"
show(qr["qr_code"], qr["recipient"])

final = client.await_payment_status(payment_id)         # PAID, CANCELED, TIMEOUTED, …
```

`await_payment_status()` has no timeout of its own — the API ends an unpaid payment on its own
schedule. Pass `timeout=` (seconds) for a client-side ceiling. It also returns on `AUTHORIZED`:
a pre-authorized payment waits for the capture, not for the poller. The v4 SDK has no capture
operation yet (see [MIGRATION.md](MIGRATION.md)).

## API

### Authentication

| Method | |
|---|---|
| `authenticate(client_id, client_secret, scope)` | Authenticate. The token is never returned. |
| `is_authenticated()` | Whether a token is held. |
| `logout()` | Forget the credentials and the token. |
| `set_shareable_key(key)` | Set the key returned by `get_browser_keys()`. |
| `get_browser_keys()` | `{"shareable_key", "client_id"}` — what the client SDK starts with. Both are safe to send to the client. |
| `with_transaction_id(transaction_id)` | A `with` block whose calls go out under your own transaction id — see [Request ids](#request-ids). |

### Payments

| Method | Endpoint |
|---|---|
| `create_payment(goid, params)` | `POST /eshops/{goid}/payments` |
| `get_payment_status(payment_id)` | `GET /payments/{payment_id}` |
| `cancel_payment(payment_id)` | `DELETE /payments/{payment_id}` — only in `CREATED`; otherwise `409` |
| `charge_payment(payment_id, params)` | `POST /payments/{payment_id}/charge` — see [Charging from the server](#charging-from-the-server) |
| `get_charge_state(payment_id)` | `GET /payments/{payment_id}/charge` |
| `get_qr_payment_info(payment_id, format=None)` | `GET /payments/{payment_id}/qr-payment/info` |
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

### Saved cards

| Method | Endpoint |
|---|---|
| `tokenize_encrypted_card(payload)` | `POST /cards/tokens` (`card:write`) |
| `get_card_details(card_id)` | `GET /cards/tokens/{card_id}` (`card:read`) |
| `delete_card(card_id)` | `DELETE /cards/tokens/{card_id}` (`card:write`) |

### Webhooks and events

| Method | Endpoint |
|---|---|
| `verify_webhook(raw_body, signature_header)` | checks a webhook and returns its event (`shared:read`) |
| `get_webhook_public_keys()` | `GET /webhooks/public-keys` (`shared:read`) |
| `list_events(goid, params=None)` | `GET /eshops/{goid}/events` (`event:read`) |
| `get_event(goid, event_id)` | `GET /eshops/{goid}/events/{event_id}` (`event:read`) |
| `iter_events(goid, params=None)` | every matching event, page after page (`event:read`) |

`is_known_event(event)` is a plain function: `from gopay_payments import is_known_event`.

### Polling options

All pollers take `on_state_change` (called with every polled state), `interval` (seconds,
at least 1), and `cancel` — a `threading.Event` that stops polling when set. A poller raising
`CHARGE_TIMEOUT` or a cancelled poller raising `CHARGE_FAILED` has not changed anything on the
API side; read the resource again to see where it stands.

| Poller | Default interval | Timeout |
|---|---|---|
| `await_charge_state` | 2 s | `initial_timeout=30` — lifted once the customer is sent to 3-D Secure |
| `await_payment_status` | 3 s | none unless `timeout=` |
| `await_refund_state` | 3 s | none unless `timeout=` |
| `await_recurrence_state` | 3 s | none unless `timeout=` |

The pollers block the calling thread. In a web request, prefer reacting to the webhook and
reading the payment with `get_payment_status()` over blocking a worker while the customer pays.

## Errors

Every error the SDK raises is a `GoPayError`:

- **`GoPayHTTPError`** — the API answered with a non-2xx status. `status`, `body` (parsed JSON,
  or text), `method`, and `endpoint` (the path with ids collapsed: `/payments/{id}/charge`).
- **`GoPaySDKError`** — the SDK gave up. `error_code` is an `ErrorCode`:

| `ErrorCode` | |
|---|---|
| `AUTH_TOKEN_MISSING` | no token — call `authenticate()` |
| `AUTH_REFRESH_FAILED` | replacing an expired token failed — see below whether to call `authenticate()` again |
| `AUTH_INVALID_RESPONSE` | the token response was not JSON, or incomplete |
| `AUTH_CREDENTIALS_MISSING` | `get_browser_keys()` without a shareable key or authentication |
| `AUTH_UNAUTHORIZED` | a fresh token was rejected — usually a missing scope |
| `NETWORK_TIMEOUT` | the request timed out |
| `NETWORK_ERROR` | the connection failed; no response arrived |
| `UNEXPECTED_RESPONSE` | the API answered `2xx` with a body that is empty, not JSON, or not the expected object — or event paging that would never end |
| `CHARGE_TIMEOUT` | a poller's timeout elapsed |
| `CHARGE_FAILED` | the charge failed, a recurrence can no longer start, or polling was cancelled |
| `WEBHOOK_INVALID` | not a webhook GoPay signed — answer `4xx` |
| `WEBHOOK_KEYS_UNAVAILABLE` | the webhook could not be checked yet — answer `5xx` |
| `INVALID_CONFIG` | invalid client configuration, or `verify_webhook()` without the `webhooks` extra |
| `INVALID_ARGUMENT` | a missing or invalid argument |

An error a request raised carries that request's `trace_id` and `transaction_id` — see
[Request ids](#request-ids).

```python
from gopay_payments import ErrorCode, GoPayHTTPError, GoPaySDKError

try:
    client.cancel_payment(payment_id)
except GoPayHTTPError as error:
    if error.status == 409:
        ...  # the payment is no longer CREATED
```

**Credentials survive an outage.** When the token request fails with a timeout, a dropped
connection, `408`, `429` or a `5xx` — in `authenticate()` or while replacing an expiring token —
the client keeps the credentials and the next call obtains a token on its own. Only credentials
the token endpoint refuses (any other `4xx`) are forgotten; then call `authenticate()` with valid
ones.

Reads (`GET`) are retried twice on a `5xx`, a `429` or a dropped connection, with a growing,
randomised pause; a `429` waits as long as its `Retry-After` asks, unless that is more than ten
seconds, which comes back to you as the `429`. Nothing that changes state is retried: a charge
or refund that reached the API must not be sent twice, and a cancel the API already carried out
would answer a repeat with `409`.

## Request ids

Every request carries ids that GoPay's log records, so GoPay support can find a call:

- `X-GoPay-Trace-Id` — one per HTTP request; its retries keep it.
- `X-GoPay-Transaction-Id` — one per SDK call, shared by every request the call makes: the
  token refresh, the retries, the polls of an `await_*` method, the pages of `iter_events()`.
- `X-GoPay-Payment-Session-Id` — the payment id, on requests about one payment.

An error a request raised — an HTTP error, a timeout, a dropped connection, an unusable
answer — carries that request's ids as `trace_id` and `transaction_id`, also where no
response arrived. They are `None` on an error the SDK raised itself: an invalid argument, no
token, or the outcome of a poller such as `CHARGE_FAILED`. Quote them when you contact GoPay
support.

To find all calls of one of your requests by your own id, run them in a block:

```python
with client.with_transaction_id(request_id):     # 1–64 of A–Z a–z 0–9 . _ -
    payment = client.create_payment(goid, params)
    client.await_payment_status(payment["id"])
```

The id belongs to the running thread (it is held in a `contextvars` variable), so threads
sharing one client keep their own. A thread started inside the block does not inherit it
unless it runs in a copy of the context; `asyncio.to_thread` copies it.

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
        scope.set_extra("gopay_trace_id", error.trace_id)
        sentry_sdk.capture_exception(error)

client = GoPayClient(environment="production", on_error=report)
```

Tag `error_code`, or `status` with `endpoint` — they are what make events groupable; keep
`trace_id` as extra data, not as a tag, since every request has its own. Do not attach the
error `body` verbatim: it is the API's response and may name the customer. Never forward the
arguments you passed in either — a charge carries card data.

Debug logs (method, URL, status and the trace id of every attempt — never headers or bodies)
go to the `gopay_payments` logger.

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

The webhook signing keys are kept per client. A process that builds a new client for every
webhook fetches them every time; keep the client for the life of the process.

## Types

Request and response shapes are `TypedDict`s in `gopay_payments.types`, named after the spec's
schemas (`Payment-Create-Request` → `PaymentCreateRequest`). They help type checkers and
editors; at runtime every value is a plain `dict`.

```python
from gopay_payments.types import PaymentCreateRequest

order: PaymentCreateRequest = {...}
```

Events are `GoPayEvent`: one of the documented event types (`gopay_payments.types.Event`), or
an `UnknownEvent` for a type added after this SDK version.

## Examples

[`examples/`](examples/) has runnable scripts for a payment, a QR payment, a refund and a
recurrence against the sandbox.

## License

MIT — see [LICENSE](LICENSE).
