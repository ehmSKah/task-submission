"""Browser clients from an allowed origin can read every response of the API function, and
nothing changes for anyone else (ADR 0014)."""

from typing import Any

import pytest

from orders_api.adapters.inbound import api_handler
from orders_api.adapters.inbound.cors import (
    ALLOWED_ORIGINS_VARIABLE,
    allowed_origins,
    cors_headers,
    static_cors_headers,
)
from orders_api.adapters.inbound.http_headers import SECURITY_HEADERS
from tests.builders import an_order, rest_event
from tests.unit.events import (
    ALICE,
    create_order_event,
    get_order_event,
    headers_of,
    invoke,
    problem_of,
)
from tests.unit.fakes import WireApi

SWAGGER_UI = "http://localhost:8080"
OTHER_SITE = "https://evil.example"
EXPOSED = "ETag, RateLimit-Limit, RateLimit-Remaining, RateLimit-Reset, Retry-After, X-Request-Id"
CORS_HEADERS = {"Access-Control-Allow-Origin", "Access-Control-Expose-Headers", "Vary"}


@pytest.fixture
def cors_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_handler, "cors_origins", lambda: frozenset({SWAGGER_UI}))


def with_origin(event: dict[str, Any], origin: str, header: str = "Origin") -> dict[str, Any]:
    event["headers"][header] = origin
    event["multiValueHeaders"][header] = [origin]
    return event


def access_control(response: dict[str, Any]) -> dict[str, str]:
    return {
        name: value
        for name, value in headers_of(response).items()
        if name.startswith("Access-Control-") or name == "Vary"
    }


@pytest.mark.usefixtures("cors_allows")
def test_an_allowed_origin_may_read_the_response_and_its_headers() -> None:
    response = invoke(with_origin(rest_event("GET", "/api/v1/health"), SWAGGER_UI))

    assert response["statusCode"] == 200
    assert access_control(response) == {
        "Access-Control-Allow-Origin": SWAGGER_UI,
        "Access-Control-Expose-Headers": EXPOSED,
        "Vary": "Origin",
    }


@pytest.mark.usefixtures("cors_allows")
def test_errors_and_success_with_rate_limit_headers_carry_them_too(wire_api: WireApi) -> None:
    wire_api([an_order()], ids=["0e6bb2c6-0000-4000-8000-000000000001"])

    created = invoke(with_origin(create_order_event(), SWAGGER_UI))
    missing = invoke(
        with_origin(get_order_event("0e6bb2c6-0000-4000-8000-000000000000", ALICE), SWAGGER_UI)
    )

    assert created["statusCode"] == 201
    assert "RateLimit-Limit" in headers_of(created)
    problem_of(missing, 404, "not-found")
    for response in (created, missing):
        assert headers_of(response)["Access-Control-Allow-Origin"] == SWAGGER_UI


@pytest.mark.usefixtures("cors_allows")
def test_the_origin_header_is_matched_in_any_casing() -> None:
    """HTTP/2 clients send header names in lower case, and REST APIs pass them on as sent."""
    response = invoke(with_origin(rest_event("GET", "/api/v1/health"), SWAGGER_UI, "origin"))

    assert headers_of(response)["Access-Control-Allow-Origin"] == SWAGGER_UI


@pytest.mark.usefixtures("cors_allows")
@pytest.mark.parametrize(
    "origin", [OTHER_SITE, "http://localhost:8080/", "HTTP://LOCALHOST:8080", "null"]
)
def test_another_origin_is_never_echoed(origin: str) -> None:
    """Only `Vary`, so a cache never serves one origin's answer to another."""
    response = invoke(with_origin(rest_event("GET", "/api/v1/health"), origin))

    assert access_control(response) == {"Vary": "Origin"}


@pytest.mark.usefixtures("cors_allows")
def test_a_request_without_an_origin_gets_no_access_control_headers() -> None:
    assert access_control(invoke(rest_event("GET", "/api/v1/health"))) == {"Vary": "Origin"}


@pytest.mark.usefixtures("cors_allows")
def test_credentials_are_never_allowed() -> None:
    """Bearer tokens are a header the page sets itself, not cookies (ADR 0014)."""
    response = invoke(with_origin(rest_event("GET", "/api/v1/health"), SWAGGER_UI))

    assert "Access-Control-Allow-Credentials" not in headers_of(response)


@pytest.mark.usefixtures("cors_allows")
def test_a_failure_outside_routing_still_carries_them(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_resolve(*_: Any) -> dict[str, Any]:
        raise RuntimeError("router exploded")

    monkeypatch.setattr(api_handler.app, "resolve", broken_resolve)

    response = invoke(with_origin(rest_event("GET", "/api/v1/health"), SWAGGER_UI))

    assert response["statusCode"] == 500
    assert headers_of(response)["Access-Control-Allow-Origin"] == SWAGGER_UI


def test_without_allowed_origins_the_response_is_as_before() -> None:
    """CORS off (the default, and aws-prod): not even `Vary`, whatever the request sends."""
    response = invoke(with_origin(rest_event("GET", "/api/v1/health"), SWAGGER_UI))

    assert not CORS_HEADERS & headers_of(response).keys()
    for name, value in SECURITY_HEADERS.items():
        assert headers_of(response)[name] == value


def test_the_function_reads_the_allowed_origins_from_its_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOWED_ORIGINS_VARIABLE, f"{SWAGGER_UI},https://app.example.com")
    api_handler.cors_origins.cache_clear()
    try:
        assert api_handler.cors_origins() == {SWAGGER_UI, "https://app.example.com"}
    finally:
        api_handler.cors_origins.cache_clear()


@pytest.mark.parametrize(("value", "expected"), [(None, set()), ("", set()), (" , ", set())])
def test_no_value_allows_no_origin(value: str | None, expected: set[str]) -> None:
    assert allowed_origins(value) == expected


def test_static_headers_name_the_origin_only_when_exactly_one_is_allowed() -> None:
    """Gateway Responses and WAF's `429` can't run code, so they can't choose among several."""
    assert static_cors_headers([SWAGGER_UI]) == cors_headers({SWAGGER_UI}, SWAGGER_UI)
    assert static_cors_headers([]) == {}
    assert static_cors_headers([SWAGGER_UI, "https://app.example.com"]) == {}
