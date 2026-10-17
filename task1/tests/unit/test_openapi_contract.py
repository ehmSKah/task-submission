from collections.abc import Iterator
from typing import Any

import pytest

from orders_api.adapters.inbound.problems import ProblemType
from orders_api_infra.openapi_extensions import (
    GATEWAY_ERRORS,
    GATEWAY_STATUSES_FOR_ANY_REQUEST,
    load_spec,
)

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
    """Every order operation is limited (ADR 0006): its success answers carry the headers. Its
    `429` may come from flood protection in front of the API too, so it promises only an
    optional `Retry-After`; the function's own `RateLimit-*` are described in prose."""
    responses = operation["responses"]

    assert responses["429"] == {"$ref": "#/components/responses/TooManyRequests"}
    assert header_names(responses["429"]) == {"Retry-After", "X-Request-Id"}
    for status, response in responses.items():
        if status.startswith("2"):
            assert header_names(response) >= RATE_LIMIT_HEADERS, status


def test_health_is_not_rate_limited_by_the_api() -> None:
    """Its `429` can only come from flood protection in front of the API (ADR 0006)."""
    responses = load_spec()["paths"]["/api/v1/health"]["get"]["responses"]

    assert not header_names(responses["200"]) & RATE_LIMIT_HEADERS


def test_the_rate_limit_headers_are_required_wherever_they_are_documented() -> None:
    headers = load_spec()["components"]["headers"]

    for name in RATE_LIMIT_HEADERS:
        assert headers[name]["required"] is True
        assert headers[name]["schema"] == {"type": "integer", "minimum": 0}
    assert "required" not in headers["Retry-After"]


# What the API function itself can answer, per operation (ADRs 0001, 0004, 0006, 0007, 0009):
# its success, a malformed request (400), a missing permission (403), the rate limits (429)
# and an unexpected error (500), plus each operation's own cases.
FUNCTION_STATUSES = {
    "getHealth": {200, 500},
    "createOrder": {201, 400, 403, 409, 413, 415, 422, 429, 500},
    "listOrders": {200, 400, 403, 429, 500},
    "getOrder": {200, 400, 403, 404, 429, 500},
    "deleteOrder": {204, 400, 403, 404, 429, 500},
    "changeOrderStatus": {200, 400, 403, 404, 409, 412, 413, 415, 429, 500},
}

_SECURED_ONLY = {"UNAUTHORIZED", "AUTHORIZER_FAILURE", "AUTHORIZER_CONFIGURATION_ERROR"}
_UNKNOWN_ROUTES_ONLY = {"MISSING_AUTHENTICATION_TOKEN"}
_GENERIC = {"DEFAULT_4XX", "DEFAULT_5XX"}


def gateway_statuses(operation: dict[str, Any]) -> set[int]:
    """The statuses API Gateway can answer this operation with, before or instead of the
    function: its mapped Gateway Responses that apply, and the ones it answers generically."""
    secured = operation.get("security") != []
    required_parameters = any(
        parameter["required"] and parameter["in"] != "path"
        for parameter in operation.get("parameters", [])
    )
    applies = {
        "BAD_REQUEST_BODY": "requestBody" in operation,
        "BAD_REQUEST_PARAMETERS": required_parameters,
    }
    statuses = set(GATEWAY_STATUSES_FOR_ANY_REQUEST.values())
    for name, error in GATEWAY_ERRORS.items():
        if name in _UNKNOWN_ROUTES_ONLY | _GENERIC or (name in _SECURED_ONLY and not secured):
            continue
        if applies.get(name, True):
            assert error.problem is not None, name
            statuses.add(error.problem.status)
    return statuses


def test_every_operation_has_its_function_statuses_listed_here() -> None:
    documented = {operation["operationId"] for _, _, operation in operations()}

    assert documented == FUNCTION_STATUSES.keys()


@pytest.mark.parametrize(
    ("path", "method", "operation"),
    operations(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_operation_lists_exactly_the_status_codes_it_can_return(
    path: str, method: str, operation: dict[str, Any]
) -> None:
    """ADR 0008: every status code an operation can return is documented, from the function
    and from the gateway in front of it, and none it can't."""
    expected = FUNCTION_STATUSES[operation["operationId"]] | gateway_statuses(operation)

    assert {int(status) for status in operation["responses"]} == expected


def test_gateway_problems_are_documented_without_rate_limit_headers() -> None:
    """A status the gateway may answer on its own can't promise the function's `RateLimit-*`
    headers: the gateway sends none (ADR 0006). Only function-only responses may require them."""
    for _, _, operation in operations():
        from_the_gateway = gateway_statuses(operation)
        for status, response in operation["responses"].items():
            if int(status) in from_the_gateway:
                assert not header_names(response) & RATE_LIMIT_HEADERS, status
