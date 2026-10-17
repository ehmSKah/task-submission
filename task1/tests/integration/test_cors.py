"""Browser clients from an allowed origin can call the API and read every answer, errors from the
gateway included; any other origin can't, and clients that send no `Origin` see no change
(ADR 0014). Runs where the environment allows origins (`local`, `aws-dev`).

A preflight isn't in the published contract (CORS depends on the environment), so preflights go
through a plain client; every other request goes through the contract-checked `api` client.
"""

import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from orders_api.adapters.inbound.cors import EXPOSED_RESPONSE_HEADERS
from orders_api_infra.config import load_config
from tests.integration.assertions import assert_contract_headers, assert_problem
from tests.integration.function_logs import FunctionLogs, api_function_name
from tests.integration.requests import NEW_ORDER, bearer

OTHER_SITE = "https://evil.example"
EXPOSED = ", ".join(EXPOSED_RESPONSE_HEADERS)
ALLOWED_REQUEST_HEADERS = "Authorization, Content-Type, Idempotency-Key, If-Match"
PATHS = {
    "/api/v1/health": "GET",
    "/api/v1/orders": "GET, POST",
    "/api/v1/orders/{id}": "DELETE, GET",
    "/api/v1/orders/{id}/status": "PATCH",
}


@pytest.fixture(scope="module")
def allowed_origins(endpoints: dict[str, Any]) -> tuple[str, ...]:
    origins = load_config(endpoints["env"]).cors_allowed_origins
    if not origins:
        pytest.skip("the environment allows no browser origin, so CORS is off")
    return origins


@pytest.fixture(scope="module")
def origin(allowed_origins: tuple[str, ...]) -> str:
    return allowed_origins[0]


@pytest.fixture(scope="module")
def browser(endpoints: dict[str, Any], allowed_origins: tuple[str, ...]) -> Iterator[httpx.Client]:
    """For preflights, which the contract doesn't describe."""
    with httpx.Client(base_url=endpoints["apiUrl"], timeout=30) as client:
        yield client


@pytest.fixture
def alice(token_for: Callable[[str], str]) -> dict[str, str]:
    return bearer(token_for("alice"))


def preflight(client: httpx.Client, path: str, origin: str, method: str) -> httpx.Response:
    return client.options(
        path.replace("{id}", str(uuid.uuid4())),
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization,content-type,if-match",
        },
    )


def access_control(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower().startswith("access-control-")
    }


def assert_readable_by(response: httpx.Response, origin: str) -> None:
    assert response.headers.get("Access-Control-Allow-Origin") == origin
    assert response.headers.get("Access-Control-Expose-Headers") == EXPOSED
    assert response.headers.get("Vary") == "Origin"
    assert "Access-Control-Allow-Credentials" not in response.headers


@pytest.mark.parametrize(("path", "methods"), PATHS.items())
def test_a_preflight_from_an_allowed_origin_allows_the_paths_methods_without_a_token(
    browser: httpx.Client, origin: str, path: str, methods: str
) -> None:
    response = preflight(browser, path, origin, methods.split(", ")[-1])

    assert response.status_code == 204
    assert access_control(response) == {
        "access-control-allow-origin": origin,
        "access-control-allow-methods": methods,
        "access-control-allow-headers": ALLOWED_REQUEST_HEADERS,
        "access-control-max-age": "7200",
    }
    assert response.headers["Vary"] == "Origin"
    assert_contract_headers(response)


def test_a_preflight_finds_the_origin_in_a_lower_case_header(
    browser: httpx.Client, origin: str
) -> None:
    """As HTTP/2 clients send it."""
    response = browser.options(
        "/api/v1/orders", headers={"origin": origin, "access-control-request-method": "POST"}
    )

    assert response.headers.get("Access-Control-Allow-Origin") == origin


def test_a_preflight_never_invokes_the_api_function(
    browser: httpx.Client, api: httpx.Client, origin: str, endpoints: dict[str, Any]
) -> None:
    """API Gateway answers it, so it costs no invocation and spends no rate limit."""
    logs = FunctionLogs(api_function_name(endpoints))
    since_ms = int(time.time() * 1000) - 5_000

    answered = preflight(browser, "/api/v1/orders", origin, "POST")
    reached = api.get("/api/v1/health")

    assert answered.status_code == 204
    logged = logs.wait_for(reached.headers["X-Request-Id"], since_ms)
    assert answered.headers["X-Request-Id"] not in logged


@pytest.mark.parametrize("other", [OTHER_SITE, "http://localhost:8080/", "null"])
def test_a_preflight_from_another_origin_gets_no_cors_headers(
    browser: httpx.Client, other: str
) -> None:
    response = preflight(browser, "/api/v1/orders", other, "POST")

    assert response.status_code == 204
    assert access_control(response) == {}


def test_an_allowed_origin_reads_a_created_order_its_etag_and_rate_limits(
    api: httpx.Client, alice: dict[str, str], origin: str
) -> None:
    response = api.post("/api/v1/orders", json=NEW_ORDER, headers={**alice, "Origin": origin})

    assert response.status_code == 201
    assert_readable_by(response, origin)
    for exposed in ("ETag", "RateLimit-Limit", "X-Request-Id"):
        assert exposed in response.headers, exposed


def test_an_allowed_origin_reads_the_function_s_errors(
    api: httpx.Client, alice: dict[str, str], origin: str
) -> None:
    response = api.get(f"/api/v1/orders/{uuid.uuid4()}", headers={**alice, "Origin": origin})

    assert_problem(response, 404, "not-found")
    assert_readable_by(response, origin)


def test_an_allowed_origin_reads_the_gateway_s_errors(
    api: httpx.Client, alice: dict[str, str], origin: str, allowed_origins: tuple[str, ...]
) -> None:
    """A rejected token, an unknown path and a body the validator refuses never reach the
    function: their Gateway Responses carry the one allowed origin as a static value."""
    if len(allowed_origins) != 1:
        pytest.skip("with several allowed origins Gateway Responses name none (ADR 0014)")
    origin_only = {"Origin": origin}

    unauthorized = api.get("/api/v1/orders", headers=origin_only)
    unknown = api.get("/api/v1/nothing-here", headers=origin_only)
    invalid = api.post(
        "/api/v1/orders", json={**NEW_ORDER, "totalAmount": 1}, headers={**alice, **origin_only}
    )

    assert_problem(unauthorized, 401, "unauthorized")
    assert_problem(unknown, 404, "not-found")
    assert_problem(invalid, 400, "validation-error")
    for response in (unauthorized, unknown, invalid):
        assert_readable_by(response, origin)


def test_another_origin_is_never_named_by_the_function(
    api: httpx.Client, alice: dict[str, str]
) -> None:
    response = api.get("/api/v1/orders", headers={**alice, "Origin": OTHER_SITE})

    assert response.status_code == 200
    assert access_control(response) == {}
    assert response.headers["Vary"] == "Origin"


def test_another_origin_never_sees_itself_in_a_gateway_response(api: httpx.Client) -> None:
    """The static value names the allowed origin, so the browser keeps the answer from the page."""
    response = api.get("/api/v1/orders", headers={"Origin": OTHER_SITE})

    assert_problem(response, 401, "unauthorized")
    assert response.headers.get("Access-Control-Allow-Origin") != OTHER_SITE


def test_a_client_without_an_origin_gets_the_same_answer_as_before(
    api: httpx.Client, alice: dict[str, str], allowed_origins: tuple[str, ...]
) -> None:
    """Only `Vary: Origin` is new: nothing for a client to read or act on."""
    response = api.get("/api/v1/orders", headers=alice)

    assert response.status_code == 200
    assert access_control(response) == {}
