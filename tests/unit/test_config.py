from __future__ import annotations

from typing import Any, Callable

import httpx
import pytest

from gopay_payments import Environment, ErrorCode, GoPayClient, GoPaySDKError, __version__

from .conftest import ErrorLog, FakeGateway


def config_error(errors: ErrorLog, **kwargs: Any) -> GoPaySDKError:
    with pytest.raises(GoPaySDKError) as caught:
        GoPayClient(on_error=errors, **kwargs)
    assert caught.value.error_code is ErrorCode.INVALID_CONFIG
    assert errors.errors == [caught.value], "construction errors reach on_error too"
    return caught.value


def test_defaults_to_sandbox() -> None:
    with GoPayClient() as client:
        assert client.environment is Environment.SANDBOX
        assert client._transport.base_url == "https://gw.sandbox.gopay.com/gp-gw/api/4.0"


def test_production_base_url() -> None:
    with GoPayClient(environment="production") as client:
        assert client._transport.base_url == "https://gate.gopay.com/gp-gw/api/4.0"


def test_unknown_environment(errors: ErrorLog) -> None:
    config_error(errors, environment="staging")


@pytest.mark.parametrize(
    ("environment", "base_url"),
    [
        ("sandbox", "http://mock.example.com/api"),
        ("production", "http://localhost:4010"),
        ("sandbox", "not a url"),
        ("sandbox", "ftp://localhost/api"),
    ],
)
def test_base_url_must_be_https(errors: ErrorLog, environment: str, base_url: str) -> None:
    config_error(errors, environment=environment, base_url=base_url)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_plain_http_allowed_for_local_sandbox(host: str) -> None:
    with GoPayClient(base_url=f"http://{host}:4010/api/") as client:
        assert client._transport.base_url == f"http://{host}:4010/api"


@pytest.mark.parametrize("timeout", [0, -1, "10", True])
def test_timeout_must_be_positive(errors: ErrorLog, timeout: Any) -> None:
    config_error(errors, timeout=timeout)


def test_user_agent_names_sdk_generation_and_version() -> None:
    with GoPayClient() as client:
        assert client.user_agent == f"GoPay Python v4 {__version__}"


def test_user_agent_suffix_is_appended() -> None:
    with GoPayClient(user_agent_suffix="MyShop/2.1") as client:
        assert client.user_agent == f"GoPay Python v4 {__version__} MyShop/2.1"


@pytest.mark.parametrize("suffix", ["", "  ", "Shop\r\nX-Injected: 1"])
def test_user_agent_suffix_rejects_unsafe_values(errors: ErrorLog, suffix: str) -> None:
    config_error(errors, user_agent_suffix=suffix)


def test_close_leaves_an_injected_http_client_open(gateway: FakeGateway) -> None:
    http = httpx.Client(transport=httpx.MockTransport(gateway.handle))
    GoPayClient(http_client=http).close()
    assert not http.is_closed
    http.close()


def test_close_closes_its_own_http_client() -> None:
    client = GoPayClient()
    client.close()
    assert client._transport._http.is_closed


def test_shareable_key_must_not_be_blank(errors: ErrorLog) -> None:
    with pytest.raises(GoPaySDKError) as caught:
        GoPayClient(shareable_key=" ", on_error=errors)
    assert caught.value.error_code is ErrorCode.INVALID_ARGUMENT


def test_context_manager(make_client: Callable[..., GoPayClient]) -> None:
    with make_client() as client:
        assert isinstance(client, GoPayClient)
