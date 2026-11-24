from __future__ import annotations

import base64
import contextlib
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from gopay_payments import (
    ErrorCode,
    GoPayClient,
    GoPayHTTPError,
    GoPaySDKError,
    is_known_event,
)
from gopay_payments._events import KNOWN_EVENT_TYPES

from .conftest import ErrorLog, FakeGateway, path_of, reply

KEYS_PATH = "/webhooks/public-keys"

# Captured from a GoPay test gateway as it was delivered, not built here: the one check that
# this verifier agrees with the gateway's signer, not merely with itself. The same capture
# the JavaScript SDK tests with.
CAPTURED = json.loads(
    (Path(__file__).parent / "fixtures" / "webhook-payments-canceled.json").read_text()
)
CAPTURED_BODY = base64.b64decode(CAPTURED["body_base64"])


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class SigningKey:
    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.private.public_key().public_numbers()
        self.jwk = {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": kid,
            "n": b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": b64url(numbers.e.to_bytes(3, "big")),
        }

    def sign(self, body: bytes, header: Optional[dict[str, Any]] = None) -> str:
        protected = b64url(json.dumps(header or {"alg": "RS256", "kid": self.kid}).encode())
        signature = self.private.sign(
            f"{protected}.{b64url(body)}".encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        return f"{protected}..{b64url(signature)}"


@pytest.fixture(scope="module")
def key_a() -> SigningKey:
    return SigningKey("test-a")


@pytest.fixture(scope="module")
def key_b() -> SigningKey:
    return SigningKey("test-b")


# A well-formed protected header, so a row fails for the part it is about and not for a kid.
WITH_KID = b64url(json.dumps({"alg": "RS256", "kid": "test-a"}).encode())


def event(type_: str = "payments.paid") -> bytes:
    return json.dumps(
        {
            "id": "01M5YWPW85D1XJMZ76N7AJHY49",
            "type": type_,
            "occurred_at": "2026-10-27T14:37:27.167Z",
            "data": {"payment_id": "7639814693"},
        }
    ).encode()


class KeyServer:
    """GET /webhooks/public-keys, answering whatever ``answer`` builds at the moment."""

    def __init__(self, gateway: FakeGateway) -> None:
        self.gateway = gateway
        self.answer: Callable[[], httpx.Response] = lambda: reply(200, CAPTURED["jwks"])
        self.delay = 0.0
        # Set to hold a fetch open: ``entered`` fires once it is in flight, and it answers
        # once ``release`` is set (or after 5 s, so a broken test fails instead of hanging).
        self.entered: Optional[threading.Event] = None
        self.release: Optional[threading.Event] = None
        gateway.on("GET", KEYS_PATH, self._handle)

    def _handle(self, _request: httpx.Request) -> httpx.Response:
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(5)
        if self.delay:
            time.sleep(self.delay)
        return self.answer()

    def serve(self, *keys: Any) -> None:
        self.answer = lambda: reply(
            200, {"keys": [k.jwk if isinstance(k, SigningKey) else k for k in keys]}
        )

    def fail(self, status: int, body: Any = None) -> None:
        self.answer = lambda: reply(status, body if body is not None else {})

    @property
    def fetches(self) -> int:
        return sum(1 for r in self.gateway.requests if path_of(r) == KEYS_PATH)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def keys(gateway: FakeGateway) -> KeyServer:
    return KeyServer(gateway)


@pytest.fixture
def clock(client: GoPayClient) -> Clock:
    c = Clock()
    client._webhooks._clock = c
    return c


def code_of(call: Callable[[], Any], code: ErrorCode) -> GoPaySDKError:
    with pytest.raises(GoPaySDKError) as caught:
        call()
    assert caught.value.error_code is code, caught.value
    return caught.value


# ------------------------------------------------------- a webhook from the gateway


def test_the_captured_webhook_verifies_and_returns_its_event(
    client: GoPayClient, keys: KeyServer
) -> None:
    result = client.verify_webhook(CAPTURED_BODY, CAPTURED["x_gopay_signature"])
    assert result == json.loads(CAPTURED_BODY)
    assert result["type"] == "payments.canceled"


def test_the_keys_are_fetched_once_with_the_bearer_token(
    client: GoPayClient, gateway: FakeGateway, keys: KeyServer
) -> None:
    client.verify_webhook(CAPTURED_BODY, CAPTURED["x_gopay_signature"])
    client.verify_webhook(CAPTURED_BODY, CAPTURED["x_gopay_signature"])
    assert keys.fetches == 1
    assert gateway.last().headers["authorization"] == "Bearer token-1"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(CAPTURED_BODY.decode(), id="str"),
        pytest.param(bytearray(CAPTURED_BODY), id="bytearray"),
        pytest.param(memoryview(CAPTURED_BODY), id="memoryview"),
    ],
)
def test_the_body_may_come_as_str_bytearray_or_memoryview(
    client: GoPayClient, keys: KeyServer, body: Any
) -> None:
    assert client.verify_webhook(body, CAPTURED["x_gopay_signature"])["id"]


@pytest.mark.parametrize("container", [list, tuple])
def test_the_header_may_come_as_a_list_of_one(
    client: GoPayClient, keys: KeyServer, container: Callable[[list[str]], Any]
) -> None:
    header = container([CAPTURED["x_gopay_signature"]])
    assert client.verify_webhook(CAPTURED_BODY, header)["id"]


def test_one_changed_byte_is_refused(client: GoPayClient, keys: KeyServer) -> None:
    tampered = CAPTURED_BODY.replace(b"7639814693", b"7639814694")
    error = code_of(
        lambda: client.verify_webhook(tampered, CAPTURED["x_gopay_signature"]),
        ErrorCode.WEBHOOK_INVALID,
    )
    assert "does not match the body" in str(error)


def test_the_same_event_serialised_again_is_refused(client: GoPayClient, keys: KeyServer) -> None:
    # The signature covers bytes: a parsed and re-dumped body is a different message.
    reserialised = json.dumps(json.loads(CAPTURED_BODY)).encode()
    assert reserialised != CAPTURED_BODY
    code_of(
        lambda: client.verify_webhook(reserialised, CAPTURED["x_gopay_signature"]),
        ErrorCode.WEBHOOK_INVALID,
    )


# ------------------------------------------------------------ the signature header


@pytest.mark.parametrize(
    ("header", "reason"),
    [
        pytest.param(None, "exactly one X-GoPay-Signature", id="missing"),
        pytest.param("", "exactly one X-GoPay-Signature", id="empty"),
        pytest.param(
            [f"{WITH_KID}..c2ln", f"{WITH_KID}..c2ln"], "exactly one X-GoPay-Signature", id="two"
        ),
        pytest.param([], "exactly one X-GoPay-Signature", id="none in a list"),
        pytest.param(f"{WITH_KID}..c2ln".encode(), "exactly one X-GoPay-Signature", id="bytes"),
        pytest.param("abc", "not a detached JWS", id="not a JWS"),
        pytest.param(f"{WITH_KID}.e30.c2ln", "not a detached JWS", id="attached payload"),
        pytest.param(f"{WITH_KID}..c2ln.c2ln", "not a detached JWS", id="four parts"),
        pytest.param(f"{WITH_KID}..c2ln==", "not base64url", id="padded"),
        pytest.param(f"{WITH_KID}..c2/n+A", "not base64url", id="standard base64"),
        pytest.param(f"{WITH_KID}..c2ln\n", "not base64url", id="trailing newline"),
        pytest.param(f"{WITH_KID}..c", "not base64url", id="impossible length"),
        pytest.param(f"{b64url(b'not json')}..c2ln", "not JSON", id="header not JSON"),
        pytest.param(f"{b64url(bytes([0xFF]))}..c2ln", "not JSON", id="header not UTF-8"),
    ],
)
def test_a_malformed_header_is_refused_before_any_key_is_fetched(
    client: GoPayClient, keys: KeyServer, header: Any, reason: str
) -> None:
    error = code_of(lambda: client.verify_webhook(event(), header), ErrorCode.WEBHOOK_INVALID)
    assert reason in str(error)
    assert keys.fetches == 0


@pytest.mark.parametrize(
    "header",
    [
        pytest.param({"alg": "HS256", "kid": "test-a"}, id="another alg"),
        pytest.param({"alg": "none", "kid": "test-a"}, id="alg none"),
        pytest.param({"alg": "RS256"}, id="no kid"),
        pytest.param({"alg": "RS256", "kid": ""}, id="empty kid"),
        pytest.param({"alg": "RS256", "kid": 7}, id="numeric kid"),
        pytest.param({"alg": "RS256", "kid": "test-a", "crit": ["exp"]}, id="crit"),
        pytest.param({"alg": "RS256", "kid": "test-a", "b64": False}, id="b64"),
        pytest.param(["RS256"], id="not an object"),
    ],
)
def test_an_unsupported_jws_header_is_refused_before_any_key_is_fetched(
    client: GoPayClient, keys: KeyServer, key_a: SigningKey, header: Any
) -> None:
    signed = key_a.sign(event(), header)
    code_of(lambda: client.verify_webhook(event(), signed), ErrorCode.WEBHOOK_INVALID)
    assert keys.fetches == 0


def test_the_message_repeats_nothing_the_request_supplied(
    client: GoPayClient, keys: KeyServer, key_a: SigningKey
) -> None:
    signed = key_a.sign(event(), {"alg": "RS256", "kid": "kid-<script>"})
    keys.serve(key_a)
    error = code_of(lambda: client.verify_webhook(event(), signed), ErrorCode.WEBHOOK_INVALID)
    assert "kid-<script>" not in str(error)


# ---------------------------------------------------------------------------- keys


def test_an_unknown_kid_refetches_so_a_rotated_in_key_verifies(
    client: GoPayClient, keys: KeyServer, clock: Clock, key_a: SigningKey, key_b: SigningKey
) -> None:
    keys.serve(key_a)
    client.verify_webhook(event(), key_a.sign(event()))
    clock.now += 60
    keys.serve(key_a, key_b)
    assert client.verify_webhook(event(), key_b.sign(event()))
    assert keys.fetches == 2


def test_an_unknown_kid_within_ten_seconds_is_not_checked_yet_and_not_refetched(
    client: GoPayClient, keys: KeyServer, clock: Clock, key_a: SigningKey, key_b: SigningKey
) -> None:
    # A real webhook signed with a key rotated in meanwhile must get a 5xx GoPay retries,
    # not a 4xx that drops it.
    keys.serve(key_a)
    client.verify_webhook(event(), key_a.sign(event()))
    clock.now += 9.999
    code_of(
        lambda: client.verify_webhook(event(), key_b.sign(event())),
        ErrorCode.WEBHOOK_KEYS_UNAVAILABLE,
    )
    assert keys.fetches == 1


def test_a_key_gopay_no_longer_publishes_stops_verifying(
    client: GoPayClient, keys: KeyServer, clock: Clock, key_a: SigningKey, key_b: SigningKey
) -> None:
    keys.serve(key_a)
    client.verify_webhook(event(), key_a.sign(event()))
    clock.now += 10
    keys.serve(key_b)
    client.verify_webhook(event(), key_b.sign(event()))
    clock.now += 10
    error = code_of(
        lambda: client.verify_webhook(event(), key_a.sign(event())), ErrorCode.WEBHOOK_INVALID
    )
    assert "does not publish" in str(error)


def test_keys_older_than_an_hour_are_refetched_so_a_withdrawn_key_stops_verifying(
    client: GoPayClient, keys: KeyServer, clock: Clock, key_a: SigningKey, key_b: SigningKey
) -> None:
    keys.serve(key_a)
    client.verify_webhook(event(), key_a.sign(event()))
    keys.serve(key_b)
    clock.now += 3600 - 0.001
    client.verify_webhook(event(), key_a.sign(event()))
    assert keys.fetches == 1
    clock.now += 0.001
    code_of(lambda: client.verify_webhook(event(), key_a.sign(event())), ErrorCode.WEBHOOK_INVALID)
    assert keys.fetches == 2


def test_the_last_keys_keep_verifying_when_the_hourly_refetch_fails(
    client: GoPayClient, keys: KeyServer, clock: Clock, key_a: SigningKey
) -> None:
    keys.serve(key_a)
    client.verify_webhook(event(), key_a.sign(event()))
    keys.fail(500)
    clock.now += 3600
    assert client.verify_webhook(event(), key_a.sign(event()))


def test_a_failed_refresh_is_retried_after_ten_seconds_not_another_hour(
    client: GoPayClient, keys: KeyServer, clock: Clock, key_a: SigningKey, key_b: SigningKey
) -> None:
    keys.serve(key_a)
    client.verify_webhook(event(), key_a.sign(event()))
    clock.now += 3600
    keys.fail(500)
    client.verify_webhook(event(), key_a.sign(event()))
    after_failure = keys.fetches
    clock.now += 9.999
    client.verify_webhook(event(), key_a.sign(event()))
    assert keys.fetches == after_failure
    clock.now += 0.001
    keys.serve(key_b)
    # Refreshed at last: key A was withdrawn meanwhile and stops verifying.
    code_of(lambda: client.verify_webhook(event(), key_a.sign(event())), ErrorCode.WEBHOOK_INVALID)


def test_a_signature_by_another_key_under_a_published_kid_is_refused(
    client: GoPayClient, keys: KeyServer, key_a: SigningKey, key_b: SigningKey
) -> None:
    keys.serve(key_a)
    forged = key_b.sign(event(), {"alg": "RS256", "kid": "test-a"})
    code_of(lambda: client.verify_webhook(event(), forged), ErrorCode.WEBHOOK_INVALID)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"use": "enc"}, id="for encryption"),
        pytest.param({"alg": "RS512"}, id="another algorithm"),
        pytest.param({"kty": "EC"}, id="another key type"),
    ],
)
def test_a_key_marked_for_something_else_is_ignored(
    client: GoPayClient,
    keys: KeyServer,
    key_a: SigningKey,
    key_b: SigningKey,
    change: dict[str, str],
) -> None:
    # Key A's own material each time, so only the marking can be what refuses it. Key B
    # beside it: a set with no usable key at all is a failed fetch, tested below.
    keys.serve({**key_a.jwk, **change}, key_b)
    code_of(lambda: client.verify_webhook(event(), key_a.sign(event())), ErrorCode.WEBHOOK_INVALID)


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param({"n": "!"}, id="modulus not base64url"),
        pytest.param({"e": "AA"}, id="zero exponent"),
        pytest.param({"n": 5}, id="numeric modulus"),
        pytest.param({"kid": 7}, id="numeric kid"),
    ],
)
def test_a_key_that_does_not_load_is_skipped_and_the_rest_kept(
    client: GoPayClient,
    keys: KeyServer,
    key_a: SigningKey,
    key_b: SigningKey,
    broken: dict[str, Any],
) -> None:
    keys.serve({**key_a.jwk, **broken}, "not a key", key_b)
    assert client.verify_webhook(event(), key_b.sign(event()))


NO_USABLE_KEY = [
    pytest.param(lambda _a: reply(200, {}), id="empty object"),
    pytest.param(lambda _a: reply(200, {"keys": []}), id="empty key list"),
    pytest.param(lambda _a: reply(200, {"keys": "none"}), id="keys not a list"),
    pytest.param(lambda a: reply(200, {"keys": [{**a.jwk, "use": "enc"}]}), id="only unusable"),
]


@pytest.mark.parametrize("answer", NO_USABLE_KEY)
def test_a_first_fetch_with_no_usable_key_leaves_the_keys_unavailable(
    client: GoPayClient, keys: KeyServer, key_a: SigningKey, answer: Callable[[SigningKey], Any]
) -> None:
    # A 200 that leaves nothing to verify with must not read as "every webhook is forged":
    # WEBHOOK_INVALID is a 4xx, and GoPay does not retry one.
    keys.answer = lambda: answer(key_a)
    code_of(
        lambda: client.verify_webhook(event(), key_a.sign(event())),
        ErrorCode.WEBHOOK_KEYS_UNAVAILABLE,
    )


@pytest.mark.parametrize("answer", NO_USABLE_KEY)
def test_a_refresh_with_no_usable_key_keeps_the_last_keys_and_retries_in_ten_seconds(
    client: GoPayClient,
    keys: KeyServer,
    clock: Clock,
    key_a: SigningKey,
    key_b: SigningKey,
    answer: Callable[[SigningKey], Any],
) -> None:
    keys.serve(key_a)
    client.verify_webhook(event(), key_a.sign(event()))
    clock.now += 3600
    keys.answer = lambda: answer(key_a)
    # The last set still verifies; a kid it lacks is not checked yet, a 5xx.
    assert client.verify_webhook(event(), key_a.sign(event()))
    code_of(
        lambda: client.verify_webhook(event(), key_b.sign(event())),
        ErrorCode.WEBHOOK_KEYS_UNAVAILABLE,
    )
    after_failure = keys.fetches
    clock.now += 10
    keys.serve(key_a, key_b)
    assert client.verify_webhook(event(), key_b.sign(event()))
    assert keys.fetches == after_failure + 1


def test_webhooks_arriving_together_share_one_fetch(
    client: GoPayClient, keys: KeyServer, key_a: SigningKey
) -> None:
    keys.serve(key_a)
    keys.delay = 0.05  # the first fetch is still running when the others ask
    header = key_a.sign(event())
    results: list[Any] = []
    threads = [
        threading.Thread(target=lambda: results.append(client.verify_webhook(event(), header)))
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 3
    assert keys.fetches == 1


def test_the_key_request_is_sent_once_not_retried(
    client: GoPayClient, keys: KeyServer, key_a: SigningKey
) -> None:
    # Webhooks arriving meanwhile wait on the lock; GoPay retries the 5xx they get anyway.
    keys.fail(503)
    code_of(
        lambda: client.verify_webhook(event(), key_a.sign(event())),
        ErrorCode.WEBHOOK_KEYS_UNAVAILABLE,
    )
    assert keys.fetches == 1


def test_a_known_fresh_key_does_not_wait_for_a_refetch_someone_else_started(
    client: GoPayClient, keys: KeyServer, clock: Clock, key_a: SigningKey, key_b: SigningKey
) -> None:
    keys.serve(key_a)
    body = event()
    genuine, made_up_kid = key_a.sign(body), key_b.sign(body)
    client.verify_webhook(body, genuine)
    clock.now += 10  # an unknown kid may refetch again
    keys.entered, keys.release = threading.Event(), threading.Event()
    refetch = threading.Thread(target=lambda: suppress_sdk_error(client, body, made_up_kid))
    refetch.start()
    try:
        assert keys.entered.wait(5)  # the refetch holds the lock now
        assert client.verify_webhook(body, genuine)["id"]
        # Answered while the refetch is still waiting for its keys, not after it.
        assert refetch.is_alive()
    finally:
        keys.release.set()
        refetch.join(5)


def suppress_sdk_error(client: GoPayClient, body: bytes, header: str) -> None:
    with contextlib.suppress(GoPaySDKError):
        client.verify_webhook(body, header)


def test_keys_that_cannot_be_loaded_are_unavailable_not_a_forged_webhook(
    client: GoPayClient, keys: KeyServer, key_a: SigningKey
) -> None:
    keys.fail(500, {"errors": [{"message": "signing keys are not available"}]})
    error = code_of(
        lambda: client.verify_webhook(event(), key_a.sign(event())),
        ErrorCode.WEBHOOK_KEYS_UNAVAILABLE,
    )
    assert isinstance(error.__cause__, GoPayHTTPError)
    assert error.__cause__.status == 500


def test_they_stay_unavailable_within_ten_seconds_without_calling_gopay_again(
    client: GoPayClient, keys: KeyServer, clock: Clock, key_a: SigningKey
) -> None:
    keys.fail(500)
    code_of(
        lambda: client.verify_webhook(event(), key_a.sign(event())),
        ErrorCode.WEBHOOK_KEYS_UNAVAILABLE,
    )
    after_first = keys.fetches  # a 5xx GET is retried, so one load is several fetches
    clock.now += 5
    code_of(
        lambda: client.verify_webhook(event(), key_a.sign(event())),
        ErrorCode.WEBHOOK_KEYS_UNAVAILABLE,
    )
    assert keys.fetches == after_first


def test_verification_recovers_once_the_keys_load_again(
    client: GoPayClient, keys: KeyServer, clock: Clock, key_a: SigningKey
) -> None:
    keys.fail(500)
    code_of(
        lambda: client.verify_webhook(event(), key_a.sign(event())),
        ErrorCode.WEBHOOK_KEYS_UNAVAILABLE,
    )
    keys.serve(key_a)
    clock.now += 60
    assert client.verify_webhook(event(), key_a.sign(event()))


@pytest.mark.parametrize(
    "refusal",
    [pytest.param((403,), id="403"), pytest.param((401,), id="401 after a fresh token")],
)
def test_the_missing_scope_is_named_when_the_token_may_not_read_the_keys(
    client: GoPayClient, keys: KeyServer, key_a: SigningKey, refusal: tuple[int]
) -> None:
    keys.fail(*refusal)
    error = code_of(
        lambda: client.verify_webhook(event(), key_a.sign(event())),
        ErrorCode.WEBHOOK_KEYS_UNAVAILABLE,
    )
    assert "shared:read" in str(error)


def test_the_message_names_no_scope_for_a_failure_of_another_kind(
    client: GoPayClient, keys: KeyServer, key_a: SigningKey
) -> None:
    keys.fail(500)
    error = code_of(
        lambda: client.verify_webhook(event(), key_a.sign(event())),
        ErrorCode.WEBHOOK_KEYS_UNAVAILABLE,
    )
    assert "shared:read" not in str(error)


# ---------------------------------------------------------------------------- body


def test_an_event_type_the_sdk_does_not_know_comes_back_unchanged(
    client: GoPayClient, keys: KeyServer, key_a: SigningKey
) -> None:
    keys.serve(key_a)
    body = event("payments.something_new")
    result = client.verify_webhook(body, key_a.sign(body))
    assert result == json.loads(body)
    assert not is_known_event(result)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not json", id="not JSON"),
        pytest.param(b"\xff", id="not UTF-8"),
        pytest.param(b"[]", id="an array"),
        pytest.param(
            json.dumps({"id": "e", "type": "payments.paid", "occurred_at": "t"}).encode(),
            id="missing data",
        ),
        pytest.param(
            json.dumps({"id": 1, "type": "payments.paid", "occurred_at": "t", "data": {}}).encode(),
            id="numeric id",
        ),
        pytest.param(
            json.dumps(
                {"id": "e", "type": "payments.paid", "occurred_at": "t", "data": []}
            ).encode(),
            id="data not an object",
        ),
    ],
)
def test_a_signed_body_that_is_not_an_event_is_refused(
    client: GoPayClient, keys: KeyServer, key_a: SigningKey, body: bytes
) -> None:
    keys.serve(key_a)
    error = code_of(
        lambda: client.verify_webhook(body, key_a.sign(body)), ErrorCode.WEBHOOK_INVALID
    )
    assert "the body is not" in str(error)


def test_a_parsed_body_is_refused_before_any_key_is_fetched(
    client: GoPayClient, keys: KeyServer, errors: ErrorLog, key_a: SigningKey
) -> None:
    error = code_of(
        lambda: client.verify_webhook(json.loads(event()), key_a.sign(event())),
        ErrorCode.INVALID_ARGUMENT,
    )
    assert "request.body" in str(error)
    assert keys.fetches == 0
    assert errors.errors == [error]


def test_without_cryptography_the_error_says_which_extra_to_install(
    client: GoPayClient, keys: KeyServer, errors: ErrorLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.asymmetric.rsa", None)
    error = code_of(
        lambda: client.verify_webhook(CAPTURED_BODY, CAPTURED["x_gopay_signature"]),
        ErrorCode.INVALID_CONFIG,
    )
    assert "gopay-payments-v4[webhooks]" in str(error)
    assert errors.errors == [error]
    assert keys.fetches == 0


# ------------------------------------------------------------------------ on_error


def test_on_error_does_not_hear_of_forged_webhooks(
    client: GoPayClient,
    keys: KeyServer,
    clock: Clock,
    errors: ErrorLog,
    key_a: SigningKey,
    key_b: SigningKey,
) -> None:
    keys.serve(key_a)
    body = event()
    forged = key_b.sign(body, {"alg": "RS256", "kid": "test-a"})
    unknown_kid = key_b.sign(body)
    for _ in range(2):
        for header in (forged, unknown_kid, "garbage"):
            with pytest.raises(GoPaySDKError):
                client.verify_webhook(body, header)
        clock.now += 10  # throttled and not
    assert errors.errors == []


def test_on_error_hears_of_a_key_set_with_no_usable_key_once_per_attempt(
    client: GoPayClient, keys: KeyServer, clock: Clock, errors: ErrorLog, key_a: SigningKey
) -> None:
    keys.serve({**key_a.jwk, "use": "enc"})
    body, header = event(), key_a.sign(event())
    for _ in range(3):
        with pytest.raises(GoPaySDKError):
            client.verify_webhook(body, header)
    assert [e.error_code for e in errors.errors] == [ErrorCode.UNEXPECTED_RESPONSE]  # type: ignore[attr-defined]
    assert errors.errors[0].trace_id is not None
    clock.now += 10
    with pytest.raises(GoPaySDKError):
        client.verify_webhook(body, header)
    assert len(errors.errors) == 2


def test_on_error_hears_of_a_failed_key_request_once_per_attempt(
    client: GoPayClient, keys: KeyServer, errors: ErrorLog, key_a: SigningKey
) -> None:
    keys.fail(500)
    body, header = event(), key_a.sign(event())
    for _ in range(3):
        with pytest.raises(GoPaySDKError):
            client.verify_webhook(body, header)
    assert len(errors.errors) == 1
    assert isinstance(errors.errors[0], GoPayHTTPError)


def test_on_error_hears_of_keys_requested_without_a_token(
    make_client: Callable[..., GoPayClient], keys: KeyServer, errors: ErrorLog, key_a: SigningKey
) -> None:
    client = make_client()
    code_of(
        lambda: client.verify_webhook(event(), key_a.sign(event())),
        ErrorCode.WEBHOOK_KEYS_UNAVAILABLE,
    )
    assert [e.error_code for e in errors.errors] == [ErrorCode.AUTH_TOKEN_MISSING]  # type: ignore[attr-defined]


# ------------------------------------------------------------- the other methods


def test_get_webhook_public_keys_returns_the_jwk_set(
    client: GoPayClient, gateway: FakeGateway, keys: KeyServer
) -> None:
    assert client.get_webhook_public_keys() == CAPTURED["jwks"]
    assert path_of(gateway.last()) == KEYS_PATH


def test_is_known_event_knows_the_twelve_documented_types() -> None:
    assert len(KNOWN_EVENT_TYPES) == 12
    for type_ in KNOWN_EVENT_TYPES:
        assert is_known_event(json.loads(event(type_)))
    assert not is_known_event(json.loads(event("payments.new")))
