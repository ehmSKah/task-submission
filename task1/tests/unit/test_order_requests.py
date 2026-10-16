"""Code-level request checks of the API function, in pipeline order (ADRs 0007 and 0009)."""

import json
from typing import Any

import pytest
from aws_lambda_powertools.utilities.data_classes import APIGatewayProxyEvent

from orders_api.adapters.inbound.order_requests import new_order_from, valid_order_id
from orders_api.adapters.inbound.problems import FieldError, ProblemType
from orders_api.adapters.inbound.request_checks import MAX_BODY_BYTES, InvalidRequest
from orders_api.domain.order import LineItem, NewOrder
from tests.unit.events import VALID_ORDER, create_order_event

ITEM = {"sku": "ABC-123", "quantity": 2, "unitPrice": 1999}


def parse(event: dict[str, Any]) -> NewOrder:
    return new_order_from(APIGatewayProxyEvent(event))


def rejection(event: dict[str, Any]) -> InvalidRequest:
    with pytest.raises(InvalidRequest) as raised:
        parse(event)
    return raised.value


def test_a_valid_body_becomes_a_typed_new_order() -> None:
    assert parse(create_order_event()) == NewOrder(
        currency="EUR",
        items=(
            LineItem(sku="ABC-123", quantity=2, unit_price=1999),
            LineItem(sku="XYZ-9", quantity=1, unit_price=500),
        ),
    )


@pytest.mark.parametrize(
    "content_type", ["application/json; charset=utf-8", "Application/JSON", "application/json "]
)
def test_json_content_type_is_matched_without_parameters_or_case(content_type: str) -> None:
    assert parse(create_order_event(content_type=content_type)).currency == "EUR"


def test_base64_encoded_body_is_decoded() -> None:
    assert parse(create_order_event(base64_encoded=True)).currency == "EUR"


@pytest.mark.parametrize("content_type", ["text/plain", "application/xml", "application/jsonx"])
def test_other_content_types_are_415(content_type: str) -> None:
    error = rejection(create_order_event(content_type=content_type))

    assert error.problem is ProblemType.UNSUPPORTED_MEDIA_TYPE


def test_missing_content_type_is_415() -> None:
    assert rejection(create_order_event(content_type=None)).problem is (
        ProblemType.UNSUPPORTED_MEDIA_TYPE
    )


def test_content_type_is_checked_before_the_body() -> None:
    error = rejection(create_order_event(raw="not json", content_type="text/plain"))

    assert error.problem is ProblemType.UNSUPPORTED_MEDIA_TYPE


def padded(size: int) -> str:
    """Valid JSON of exactly `size` bytes: whitespace passes any schema."""
    text = json.dumps(VALID_ORDER)
    return text + " " * (size - len(text))


def test_body_of_exactly_64_kb_is_accepted() -> None:
    assert parse(create_order_event(raw=padded(MAX_BODY_BYTES))).currency == "EUR"


def test_body_over_64_kb_is_413() -> None:
    assert rejection(create_order_event(raw=padded(MAX_BODY_BYTES + 1))).problem is (
        ProblemType.PAYLOAD_TOO_LARGE
    )


def test_body_size_counts_bytes_not_characters() -> None:
    text = json.dumps(VALID_ORDER)
    multibyte = text + " " * (MAX_BODY_BYTES - len(text) - 1) + "€"  # 3 bytes in UTF-8

    assert rejection(create_order_event(raw=multibyte)).problem is ProblemType.PAYLOAD_TOO_LARGE


def test_body_size_is_checked_before_parsing() -> None:
    error = rejection(create_order_event(raw="x" * (MAX_BODY_BYTES + 1)))

    assert error.problem is ProblemType.PAYLOAD_TOO_LARGE


@pytest.mark.parametrize("raw", ["", "   ", '{"currency": "EUR",', "NaN", "{'a': 1}"])
def test_empty_or_non_json_body_is_400(raw: str) -> None:
    error = rejection(create_order_event(raw=raw))

    assert error.problem is ProblemType.VALIDATION_ERROR
    assert not error.errors


@pytest.mark.parametrize("field", ["totalAmount", "ownerId", "id", "status"])
def test_server_owned_fields_are_unknown_fields(field: str) -> None:
    error = rejection(create_order_event({**VALID_ORDER, field: "x"}))

    assert error.problem is ProblemType.VALIDATION_ERROR
    assert error.errors == (
        FieldError(pointer="", detail=f"must not contain {{'{field}'}} properties"),
    )


def test_schema_violation_points_at_the_field() -> None:
    error = rejection(
        create_order_event({"currency": "EUR", "items": [{**ITEM, "quantity": 1000}]})
    )

    assert error.problem is ProblemType.VALIDATION_ERROR
    assert error.detail == (
        "Invalid request body: $.items[0].quantity: must be smaller than or equal to 999"
    )
    assert error.errors == (
        FieldError(pointer="/items/0/quantity", detail="must be smaller than or equal to 999"),
    )


@pytest.mark.parametrize(
    "body",
    [
        {"currency": "CHF", "items": [ITEM]},
        {"currency": "EUR", "items": []},
        {"currency": "EUR", "items": [ITEM] * 51},
        {"currency": "EUR", "items": [{**ITEM, "sku": "lower-case"}]},
        {"currency": "EUR", "items": [{**ITEM, "unitPrice": 19.99}]},
        {"currency": "EUR", "items": [{**ITEM, "quantity": "2"}]},
        {"currency": "EUR"},
        [VALID_ORDER],
    ],
)
def test_bodies_breaking_the_contract_are_400(body: Any) -> None:
    error = rejection(create_order_event(body))

    assert error.problem is ProblemType.VALIDATION_ERROR
    assert len(error.errors) == 1


def test_duplicate_skus_are_400_pointing_at_each_repeat() -> None:
    other = {**ITEM, "sku": "XYZ-9"}
    body = {"currency": "EUR", "items": [ITEM, other, {**ITEM, "quantity": 5}, ITEM]}

    error = rejection(create_order_event(body))

    assert error.problem is ProblemType.VALIDATION_ERROR
    assert error.detail == "Each SKU may appear only once per order"
    assert error.errors == (
        FieldError(pointer="/items/2/sku", detail="duplicates the SKU at /items/0/sku"),
        FieldError(pointer="/items/3/sku", detail="duplicates the SKU at /items/0/sku"),
    )


def test_schema_is_checked_before_duplicate_skus() -> None:
    body = {"currency": "CHF", "items": [ITEM, ITEM]}

    assert rejection(create_order_event(body)).errors[0].pointer == "/currency"


def test_well_formed_order_id_passes() -> None:
    assert valid_order_id("0e6bb2c6-0000-4000-8000-000000000000") == (
        "0e6bb2c6-0000-4000-8000-000000000000"
    )


@pytest.mark.parametrize(
    "order_id",
    [
        "not-a-uuid",
        "0E6BB2C6-0000-4000-8000-000000000000",
        "0e6bb2c6-0000-1000-8000-000000000000",
        "0e6bb2c6000040008000000000000000",
    ],
)
def test_malformed_order_id_is_400_naming_the_parameter(order_id: str) -> None:
    with pytest.raises(InvalidRequest) as raised:
        valid_order_id(order_id)

    error = raised.value
    assert error.problem is ProblemType.VALIDATION_ERROR
    assert error.detail == "Invalid path parameter: id"
    assert [(e.parameter, e.pointer) for e in error.errors] == [("id", None)]
