from __future__ import annotations

import json
import threading
from collections import defaultdict, deque
from collections.abc import Iterator
from typing import Any, Callable, Optional, Union
from urllib.parse import parse_qs

import httpx
import pytest

from gopay_payments import GoPayClient, GoPayError, _polling

BASE = "https://gw.sandbox.gopay.com/gp-gw/api/4.0"
PREFIX = "/gp-gw/api/4.0"
TOKEN_PATH = "/oauth2/token"

Reply = Union[httpx.Response, Exception, Callable[[httpx.Request], httpx.Response]]


def reply(status: int = 200, body: Any = None, *, text: Optional[str] = None) -> httpx.Response:
    if text is not None:
        return httpx.Response(status, text=text, headers={"content-type": "text/plain"})
    if body is None:
        return httpx.Response(status)
    return httpx.Response(status, json=body)


class FakeGateway:
    """An in-memory Payments API: queued replies per route, every request recorded."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: dict[tuple[str, str], deque[Reply]] = defaultdict(deque)
        self._lock = threading.Lock()
        self.token_counter = 0

    def on(self, method: str, path: str, *replies: Reply) -> None:
        self._routes[(method, path)].extend(replies)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        with self._lock:
            self.requests.append(request)
            queue = self._routes.get((request.method, path))
            if queue:
                # The last reply repeats, so a route answering the same way every time
                # needs registering only once.
                item = queue.popleft() if len(queue) > 1 else queue[0]
            elif request.method == "POST" and path == TOKEN_PATH:
                self.token_counter += 1
                item = reply(
                    200, {"access_token": f"token-{self.token_counter}", "expires_in": 900}
                )
            else:
                item = reply(404, {"errors": [{"error_code": 404, "message": "no route"}]})
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        return item

    # ----------------------------------------------------------------- helpers

    def api_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if not r.url.path.endswith(TOKEN_PATH)]

    def token_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(TOKEN_PATH)]

    def last(self) -> httpx.Request:
        return self.api_requests()[-1]


def path_of(request: httpx.Request) -> str:
    raw = request.url.raw_path.decode()
    return raw[len(PREFIX) :] if raw.startswith(PREFIX) else raw


def json_of(request: httpx.Request) -> Any:
    return json.loads(request.content) if request.content else None


def form_of(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


class ErrorLog:
    def __init__(self) -> None:
        self.errors: list[GoPayError] = []

    def __call__(self, error: GoPayError) -> None:
        self.errors.append(error)


@pytest.fixture(autouse=True)
def instant_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pollers pause at least a second between polls; tests should not."""

    def pause(_seconds: float, cancel: Optional[threading.Event]) -> bool:
        return cancel is not None and cancel.is_set()

    monkeypatch.setattr(_polling, "_pause", pause)


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def errors() -> ErrorLog:
    return ErrorLog()


@pytest.fixture
def make_client(gateway: FakeGateway, errors: ErrorLog) -> Iterator[Callable[..., GoPayClient]]:
    created: list[GoPayClient] = []

    def factory(**kwargs: Any) -> GoPayClient:
        kwargs.setdefault(
            "http_client", httpx.Client(transport=httpx.MockTransport(gateway.handle))
        )
        kwargs.setdefault("on_error", errors)
        client = GoPayClient(**kwargs)
        client._transport._sleep = lambda _seconds: None
        created.append(client)
        return client

    yield factory
    for client in created:
        client.close()


@pytest.fixture
def client(make_client: Callable[..., GoPayClient]) -> GoPayClient:
    c = make_client()
    c.authenticate("client-id", "client-secret", "payment:write payment:read")
    return c
