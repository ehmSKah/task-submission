from typing import Any

import fastjsonschema
import pytest

from orders_api.contract import (
    DRAFT_04,
    load_contract,
    path_parameter_schema,
    property_schema,
    query_parameter_schemas,
    request_body_schema,
)

VALID_ORDER = {"currency": "EUR", "items": [{"sku": "ABC-123", "quantity": 2, "unitPrice": 1999}]}


def operations_with_a_body() -> list[tuple[str, str]]:
    return [
        (method, path)
        for path, path_item in load_contract()["paths"].items()
        for method, operation in path_item.items()
        if isinstance(operation, dict) and "requestBody" in operation
    ]


def test_there_is_at_least_one_request_body_to_check() -> None:
    assert ("post", "/api/v1/orders") in operations_with_a_body()


@pytest.mark.parametrize(("method", "path"), operations_with_a_body())
def test_every_request_schema_compiles_as_draft_04(method: str, path: str) -> None:
    schema = request_body_schema(method, path)

    assert schema["$schema"] == DRAFT_04
    fastjsonschema.compile(schema)


def parameter_schemas() -> list[tuple[str, dict[str, Any]]]:
    """Every path and query parameter schema the API function compiles (ADR 0007)."""
    found = []
    for path, path_item in load_contract()["paths"].items():
        for parameter in path_item.get("parameters", []):
            if parameter["in"] == "path":
                found.append(
                    (f"{path} {parameter['name']}", path_parameter_schema(path, parameter["name"]))
                )
        for method, operation in path_item.items():
            if isinstance(operation, dict) and "responses" in operation:
                for name, schema in query_parameter_schemas(method, path).items():
                    found.append((f"{method} {path} ?{name}", schema))
    return found


def test_there_are_path_and_query_parameters_to_check() -> None:
    names = {where for where, _ in parameter_schemas()}

    assert {"/api/v1/orders/{id} id", "get /api/v1/orders ?limit"} <= names


@pytest.mark.parametrize(
    ("where", "schema"),
    parameter_schemas(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_parameter_schema_compiles_as_draft_04(where: str, schema: dict[str, Any]) -> None:
    assert schema["$schema"] == DRAFT_04
    fastjsonschema.compile(schema)


def check(body: Any) -> None:
    fastjsonschema.compile(request_body_schema("post", "/api/v1/orders"))(body)


def test_create_order_schema_accepts_a_valid_order() -> None:
    check(VALID_ORDER)


@pytest.mark.parametrize(
    "body",
    [
        {**VALID_ORDER, "totalAmount": 3998},
        {**VALID_ORDER, "ownerId": "mallory"},
        {**VALID_ORDER, "currency": "CHF"},
        {**VALID_ORDER, "items": []},
        {**VALID_ORDER, "items": [VALID_ORDER["items"][0]] * 51},
        {"currency": "EUR", "items": [{"sku": "abc", "quantity": 1, "unitPrice": 1}]},
        {"currency": "EUR", "items": [{"sku": "ABC-1\n", "quantity": 1, "unitPrice": 1}]},
        {"currency": "EUR", "items": [{"sku": "ABC-1", "quantity": 0, "unitPrice": 1}]},
        {"currency": "EUR", "items": [{"sku": "ABC-1", "quantity": 1000, "unitPrice": 1}]},
        {"currency": "EUR", "items": [{"sku": "ABC-1", "quantity": 1.5, "unitPrice": 1}]},
        {"currency": "EUR", "items": [{"sku": "ABC-1", "quantity": 1, "unitPrice": -1}]},
        {"currency": "EUR", "items": [{"sku": "ABC-1", "quantity": 1, "unitPrice": 1_000_001}]},
        {"currency": "EUR", "items": [{"sku": "ABC-1", "quantity": 1, "unitPrice": 1, "x": 1}]},
        {"currency": "EUR"},
        [VALID_ORDER],
    ],
)
def test_create_order_schema_rejects_what_adr_0007_forbids(body: Any) -> None:
    with pytest.raises(fastjsonschema.JsonSchemaValueException):
        check(body)


def test_path_id_schema_is_the_uuid_v4_pattern() -> None:
    validate = fastjsonschema.compile(path_parameter_schema("/api/v1/orders/{id}", "id"))

    validate("0e6bb2c6-0000-4000-8000-000000000000")
    for malformed in ["not-a-uuid", "0E6BB2C6-0000-4000-8000-000000000000", ""]:
        with pytest.raises(fastjsonschema.JsonSchemaValueException):
            validate(malformed)


def test_an_order_id_property_is_the_same_uuid_as_the_path_id() -> None:
    assert (
        property_schema("Order", "id")["pattern"]
        == (path_parameter_schema("/api/v1/orders/{id}", "id")["pattern"])
    )


STATUS_PATH = "/api/v1/orders/{id}/status"


@pytest.mark.parametrize("status", ["PENDING", "CONFIRMED", "SHIPPED", "DELIVERED", "CANCELLED"])
def test_change_status_schema_accepts_each_of_the_five_statuses(status: str) -> None:
    fastjsonschema.compile(request_body_schema("patch", STATUS_PATH))({"status": status})


@pytest.mark.parametrize(
    "body",
    [
        {"status": "LOST"},
        {"status": "pending"},
        {"status": None},
        {"status": 1},
        {},
        {"status": "SHIPPED", "version": 2},
        ["SHIPPED"],
        "SHIPPED",
    ],
)
def test_change_status_schema_rejects_what_adr_0007_forbids(body: Any) -> None:
    with pytest.raises(fastjsonschema.JsonSchemaValueException):
        fastjsonschema.compile(request_body_schema("patch", STATUS_PATH))(body)


def test_the_status_path_declares_the_same_id_as_the_order_path() -> None:
    assert path_parameter_schema(STATUS_PATH, "id") == path_parameter_schema(
        "/api/v1/orders/{id}", "id"
    )


def test_unknown_operation_or_parameter_is_a_programming_error() -> None:
    with pytest.raises(KeyError):
        request_body_schema("put", "/api/v1/orders")
    with pytest.raises(KeyError):
        path_parameter_schema("/api/v1/orders/{id}", "orderId")
    with pytest.raises(KeyError):
        property_schema("Order", "secret")
