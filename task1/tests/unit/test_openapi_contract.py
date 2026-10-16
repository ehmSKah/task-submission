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
