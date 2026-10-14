# gopay-python-api-v4 — CLAUDE.md

## Project

GoPay Payments API v4 — Python server-side SDK, published as `gopay-payments-v4` and imported
as `gopay_payments`. It mirrors the server SDK in `../gp-gw-js-sdk` (`sdk/` + `internal/core`)
and `../gopay-php-api-v4`: same operations, same error codes, same polling semantics, same
wire behaviour. When the three disagree, JS is the reference for behaviour and this file says
where Python deliberately differs.

The v3 SDK (`gopay`, repo `gopay-python-api`) is a separate package and stays as it is.

## Checks

```bash
uv sync
uv run ruff format --check src tests scripts
uv run ruff check src tests scripts
uv run mypy
uv run pytest                    # unit tests; e2e is deselected by default
uv run pytest -m e2e             # live tests, needs GOPAY_PAYMENTS_V4_* (tests/e2e/conftest.py)
```

Run the suite on the floor interpreter too — mypy no longer accepts a 3.9 target, so this is
the only thing that proves 3.9 still works:

```bash
uv run --isolated --python 3.9 --with . --with pytest python -m pytest -q
```

## Architecture

```
src/gopay_payments/
├── client.py        ← GoPayClient: every public method, flat, each wrapped by _reported
├── _transport.py    ← HTTP: headers, retries, auth, token refresh, error mapping, on_error
├── _polling.py      ← await_charge / await_state (sync loops, threading.Event cancel)
├── token_cache.py   ← TokenCache protocol + InMemoryTokenCache
├── environment.py   ← Environment, base URLs, base_url validation
├── errors.py        ← ErrorCode, GoPayError, GoPaySDKError, GoPayHTTPError
├── scopes.py        ← GoPayScopes, combine_scopes
├── _validate.py     ← path segments, origins, header values
├── _endpoint.py     ← /payments/{id} normalisation for monitoring
├── _version.py      ← __version__ from package metadata, USER_AGENT
├── types.py         ← public re-export of the generated models
└── _generated.py    ← GENERATED — do not edit
```

## Models — generated

`src/gopay_payments/_generated.py` holds `TypedDict`s generated from the spec by
`datamodel-code-generator` (pinned in `scripts/codegen.py`, run through `uvx`, never a package
dependency). Never edit it by hand; if a model is wrong, fix the spec.

```bash
SPEC_SOURCE=<url-or-path> uv run scripts/codegen.py
```

`SPEC_SOURCE` is required. Models come from the **next spec** — the upcoming version, ahead of
what api-docs.gopay.com publishes — which has two sources:

1. **GoPay's internal pre-release feed**:
   `https://payments-api.beta.gopay.com/spec/en/payments.yaml`. Reachable only from inside
   GoPay, and shut down overnight and at weekends, so a 404 or timeout out of hours is expected.
2. **Sideload from the spec repo**: check out `gopay-payments-api-v4` at the commit you want
   and pass `../gopay-payments-api-v4/spec/en/payments.yaml`.

The script stages the spec, strips the mock server the feed injects as `servers[0]` (failing
closed if any trace survives), generates into `.codegen-tmp/`, and only then replaces
`_generated.py` and the vendored `spec/payments.yaml`. It refuses the vendored spec as its
own input: the copy lags the feed, and regenerating from it silently rolls the models back.
The generator is deterministic — regenerating from an unchanged spec changes nothing, so any
diff is a real spec change.

Why `TypedDict` and not pydantic: zero runtime cost, nothing to keep compatible across
pydantic majors, and pydantic 2.14 already dropped Python 3.9. The generator no longer targets
3.9 either, so it targets 3.10 without the `|` operator and takes `NotRequired`, `TypeAlias`
and friends from `typing_extensions`. The inline Google/Apple Pay info responses have no
schema of their own, so those methods return `dict[str, Any]`.

### This file is the only place the internal hostname belongs

"beta" in that hostname names the environment the spec is authored on, not the maturity of the
API, but nobody outside GoPay can tell. The repo is mirrored to the public `gopaycommunity`
organisation, so keep the hostname out of `README.md`, `MIGRATION.md`, `CHANGELOG.md`, `src/`,
`scripts/` (hence `SPEC_SOURCE`) and **commit subjects** — semantic-release copies every
`fix:`/`feat:` subject into the public changelog.

A spec update is committed as `fix: update api from next spec GPOMA-xxxx`, nothing about the
source.

## Decisions that are easy to undo by accident

- **User-Agent** is `GoPay Python v4 <version>`, on every request including the token call.
  "v4" is there because v3 sends `GoPay Python <version>` and the version ranges overlap. The
  version comes from package metadata, which the release tag sets — there is no version
  constant to bump. `user_agent_suffix` is appended, never a replacement.
- **Polling interval floor is 1 s.** The API asks to be polled about once a second; an
  `interval=0` once hammered the sandbox in a tight loop. Unit tests stub `_polling._pause`
  instead of lowering the floor.
- **POST is never retried**, only idempotent methods on 5xx or a dropped connection, and a
  timeout is never retried (the request may still be running).
- **A POST without a body still sends `Content-Type: application/json`** — the gateway
  answers 415 otherwise (`start_recurrence()` / `create_next_payment()` without overrides).
- **Path segments** are percent-encoded with `quote(..., safe="")` and `.`/`..` are refused —
  byte-identical to PHP `rawurlencode` and the JS SDK.
- **Errors reach `on_error` exactly once**, via the `_reported` decorator on every public
  method plus construction errors in `GoPayClient.__init__`. Validation errors count.
- **`await_recurrence_state` refuses to wait forever** with the default terminal states: a
  `NEW` recurrence, or a `REQUESTED` one whose first payment is canceled or timed out, raises.
  Verified live on the sandbox (14.10.2026): such a recurrence cannot be stopped (`409`) until
  its first payment is canceled.
- **`gw_url` is GoPay's hosted gateway**, a supported integration — not legacy, not a v3
  escape hatch. Drive it or `charge_payment()` per payment, never both. Recurrences use it on
  purpose.
- **`browser_data` comes from the customer's browser.** Never offer to fill it from the
  server's request.
- **README describes behaviour only** — no token lifetimes, grant names or 3-D Secure timeout
  numbers. "Redirect immediately", never "within N seconds".

## Release

semantic-release on `master` (`release.config.mjs`), tags without a `v`. `feat:` → minor,
`fix:` → patch, breaking → major. Do not write the words "breaking change" in a commit body
unless you mean it: the analyzer reads them as a major release. Telemetry, logging, examples
and docs-only fixes are `fix:`, not `feat:`.

The tag is the version (hatch-vcs). Bitbucket mirrors `master` and tags to
`github.com/gopaycommunity/gopay-python-api-v4`, where `.github/workflows/pypi-publish.yml`
publishes to PyPI through trusted publishing (no token). Publishing runs only when the GitHub
repository variable `GOPAY_PYPI_PUBLISH` is `true`.

## E2E

`uv run pytest -m e2e` against the public sandbox needs a sandbox eshop's
`GOPAY_PAYMENTS_V4_CLIENT_ID`, `_CLIENT_SECRET` and `_GOID`. Never commit them (`.env` is
ignored). The suite refuses production, bounds every poll, and creates only payments it cancels.
