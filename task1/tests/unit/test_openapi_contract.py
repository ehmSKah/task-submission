from collections.abc import Iterator
from typing import Any

import pytest

from orders_api.adapters.inbound.problems import ProblemType
from orders_api_infra.openapi_extensions import load_spec

FORBIDDEN_KEYWORDS = {"nullable", "readOnly", "writeOnly", "format"}


def test_the_contract_documents_exactly_the_problem_type_catalogue() -> None:
    documented = load_spec()["components"]["schemas"]["Problem"]["properties"]["type"]["enum"]

    assert set(documented) == {problem.urn for problem in ProblemType} | {"about:blank"}


def subschemas(schema: Any, where: str) -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(schema, dict):
        yield where, schema
        for key, value in schema.items():
            if key == "properties":
                for name, prop in value.items():
                    yield from subschemas(prop, f"{where}.{name}")
            elif key in {"items", "additionalProperties"}:
                yield from subschemas(value, f"{where}[{key}]")


def all_schemas() -> list[tuple[str, dict[str, Any]]]:
    return [
        found
        for name, schema in load_spec()["components"]["schemas"].items()
        for found in subschemas(schema, name)
    ]


def request_body_models() -> list[str]:
    return [
        operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].split("/")[-1]
        for path_item in load_spec()["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "requestBody" in operation
    ]


@pytest.mark.parametrize("model", request_body_models())
def test_request_body_schemas_are_self_contained(model: str) -> None:
    """Floci 2.3.0's validator rejects every body whose model uses a `$ref` (verification.md)."""
    schema = load_spec()["components"]["schemas"][model]

    assert all("$ref" not in found for _, found in subschemas(schema, model)), model


@pytest.mark.parametrize(("where", "schema"), all_schemas())
def test_schemas_follow_the_draft_04_safe_rules(where: str, schema: dict[str, Any]) -> None:
    """One schema serves OpenAPI, API Gateway models and fastjsonschema (ADR 0008)."""
    assert not FORBIDDEN_KEYWORDS & schema.keys(), where
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False, where
    if "$ref" in schema:
        assert schema["$ref"].startswith("#/components/schemas/"), where


RATE_LIMIT_HEADERS = {"RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset"}


def operations() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, operation)
        for path, path_item in load_spec()["paths"].items()
        for method, operation in path_item.items()
        if isinstance(operation, dict) and "responses" in operation
    ]


def resolved(response: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in response:
        resolved_response: dict[str, Any] = load_spec()["components"]["responses"][
            response["$ref"].split("/")[-1]
        ]
        return resolved_response
    return response


def header_names(response: dict[str, Any]) -> set[str]:
    return set(resolved(response).get("headers", {}))


@pytest.mark.parametrize(
    ("path", "method", "operation"),
    [op for op in operations() if op[0] != "/api/v1/health"],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_rate_limited_operation_documents_its_429_and_headers(
    path: str, method: str, operation: dict[str, Any]
) -> None:
    """Every order operation is limited (ADR 0006): its success answers carry the headers, and
    so does the `429`, with `Retry-After`."""
    responses = operation["responses"]

    assert responses["429"] == {"$ref": "#/components/responses/TooManyRequests"}
    assert header_names(responses["429"]) >= RATE_LIMIT_HEADERS | {"Retry-After"}
    for status, response in responses.items():
        if status.startswith("2"):
            assert header_names(response) >= RATE_LIMIT_HEADERS, status


def test_health_is_not_rate_limited() -> None:
    responses = load_spec()["paths"]["/api/v1/health"]["get"]["responses"]

    assert "429" not in responses
    assert not header_names(responses["200"]) & RATE_LIMIT_HEADERS


def test_the_rate_limit_headers_are_required_wherever_they_are_documented() -> None:
    headers = load_spec()["components"]["headers"]

    for name in RATE_LIMIT_HEADERS | {"Retry-After"}:
        assert headers[name]["required"] is True
        assert headers[name]["schema"] == {"type": "integer", "minimum": 0}
