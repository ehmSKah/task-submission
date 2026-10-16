import io
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from orders_api.adapters.inbound import api_handler
from orders_api.adapters.inbound.http_headers import SECURITY_HEADERS


@dataclass
class FakeLambdaContext:
    function_name: str = "orders-api"
    memory_limit_in_mb: int = 256
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:000000000000:function:orders-api"
    aws_request_id: str = "lambda-req-1"


def rest_event(method: str, path: str, resource: str | None = None) -> dict[str, Any]:
    return {
        "resource": resource or path,
        "path": path,
        "httpMethod": method,
        "headers": {"Accept": "application/json"},
        "multiValueHeaders": {"Accept": ["application/json"]},
        "queryStringParameters": None,
        "multiValueQueryStringParameters": None,
        "pathParameters": None,
        "stageVariables": None,
        "requestContext": {
            "requestId": "gw-req-1",
            "resourcePath": resource or path,
            "httpMethod": method,
            "path": f"/live{path}",
            "stage": "live",
            "identity": {"sourceIp": "203.0.113.7"},
        },
        "body": None,
        "isBase64Encoded": False,
    }


def headers_of(response: dict[str, Any]) -> dict[str, str]:
    merged = {k: v for k, v in (response.get("headers") or {}).items()}
    for key, values in (response.get("multiValueHeaders") or {}).items():
        merged[key] = values[-1]
    return merged


def invoke(event: dict[str, Any]) -> dict[str, Any]:
    response: dict[str, Any] = api_handler.handler(event, FakeLambdaContext())
    return response


def test_health_returns_ok() -> None:
    response = invoke(rest_event("GET", "/api/v1/health"))

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"status": "ok"}
    assert headers_of(response)["Content-Type"] == "application/json"


def test_every_response_carries_security_headers_and_request_id() -> None:
    headers = headers_of(invoke(rest_event("GET", "/api/v1/health")))

    for name, value in SECURITY_HEADERS.items():
        assert headers[name] == value
    assert headers["X-Request-Id"] == "gw-req-1"


def test_unknown_route_inside_the_function_is_a_problem_404() -> None:
    response = invoke(rest_event("GET", "/api/v1/nope"))

    assert response["statusCode"] == 404
    assert headers_of(response)["Content-Type"] == "application/problem+json"
    body = json.loads(response["body"])
    assert body["type"] == "urn:problem:orders-api:not-found"
    assert body["requestId"] == "gw-req-1"
    assert body["instance"] == "/api/v1/nope"


@pytest.fixture
def failing_health(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def boom() -> dict[str, str]:
        raise RuntimeError("database password is hunter2")

    monkeypatch.setattr(api_handler, "health_status", boom)
    yield


@pytest.mark.usefixtures("failing_health")
def test_unexpected_error_is_a_generic_500_without_internals() -> None:
    response = invoke(rest_event("GET", "/api/v1/health"))

    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert body["type"] == "urn:problem:orders-api:internal-error"
    assert body["detail"] == "An unexpected error occurred"
    assert "hunter2" not in response["body"]
    assert headers_of(response)["Cache-Control"] == "no-store"


def test_error_outside_routing_is_still_a_problem_500(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_resolve(*_: Any) -> dict[str, Any]:
        raise RuntimeError("router exploded")

    monkeypatch.setattr(api_handler.app, "resolve", broken_resolve)

    response = invoke(rest_event("GET", "/api/v1/health"))

    assert response["statusCode"] == 500
    assert headers_of(response)["Content-Type"] == "application/problem+json"
    assert headers_of(response)["X-Request-Id"] == "gw-req-1"
    body = json.loads(response["body"])
    assert body["type"] == "urn:problem:orders-api:internal-error"
    assert body["requestId"] == "gw-req-1"
    assert "exploded" not in response["body"]


@pytest.fixture
def log_output() -> Iterator[io.StringIO]:
    """Capture the API function's structured log output (Powertools binds stdout at import)."""
    stream = io.StringIO()
    capture = logging.StreamHandler(stream)
    capture.setFormatter(api_handler.logger.registered_formatter)
    api_handler.logger.addHandler(capture)
    yield stream
    api_handler.logger.removeHandler(capture)


def test_log_lines_carry_request_id_env_and_problem_type(
    log_output: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORDERS_API_ENV", "local")

    invoke(rest_event("GET", "/api/v1/nope"))

    lines = [json.loads(line) for line in log_output.getvalue().splitlines() if line]
    access = next(line for line in lines if line["message"] == "request")
    assert access["requestId"] == "gw-req-1"
    assert access["env"] == "local"
    assert access["route"] == "GET /api/v1/nope"
    assert access["status"] == 404
    assert access["problemType"] == "urn:problem:orders-api:not-found"
    assert "latencyMs" in access
