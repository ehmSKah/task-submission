"""Order requests, from API Gateway event to domain input (ADR 0007; pipeline steps 4-5, ADR 0009).

Rules live in the OpenAPI document. The Pydantic models below carry types only: Powertools Parser
turns an already validated body into them, so there is nothing to keep in sync.
"""

from typing import Any

from aws_lambda_powertools.utilities.data_classes.common import BaseProxyEvent
from aws_lambda_powertools.utilities.parser import parse
from pydantic import BaseModel

from orders_api.adapters.inbound.problems import FieldError, ProblemType, json_pointer
from orders_api.adapters.inbound.request_checks import (
    InvalidRequest,
    json_body,
    validate_body,
    validate_path_parameter,
)
from orders_api.contract import path_parameter_schema, request_body_schema
from orders_api.domain.order import LineItem, NewOrder

ORDERS_PATH = "/api/v1/orders"
ORDER_PATH = "/api/v1/orders/{id}"


class LineItemBody(BaseModel):
    sku: str
    quantity: int
    unitPrice: int  # the published field name


class CreateOrderBody(BaseModel):
    currency: str
    items: list[LineItemBody]


def _reject_duplicate_skus(body: dict[str, Any]) -> None:
    """JSON Schema's `uniqueItems` compares whole items, so it can't express this (ADR 0007)."""
    first_seen: dict[str, int] = {}
    errors = []
    for index, item in enumerate(body["items"]):
        first = first_seen.setdefault(item["sku"], index)
        if first != index:
            errors.append(
                FieldError(
                    pointer=json_pointer(["items", index, "sku"]),
                    detail=f"duplicates the SKU at {json_pointer(['items', first, 'sku'])}",
                )
            )
    if errors:
        raise InvalidRequest(
            ProblemType.VALIDATION_ERROR, "Each SKU may appear only once per order", errors
        )


def new_order_from(event: BaseProxyEvent) -> NewOrder:
    """`Content-Type` → size → JSON → schema → duplicate SKUs → typed model."""
    body = json_body(event)
    validate_body(body, request_body_schema("post", ORDERS_PATH))
    _reject_duplicate_skus(body)
    parsed = parse(event=body, model=CreateOrderBody)
    return NewOrder(
        currency=parsed.currency,
        items=tuple(
            LineItem(sku=item.sku, quantity=item.quantity, unit_price=item.unitPrice)
            for item in parsed.items
        ),
    )


def valid_order_id(order_id: str) -> str:
    """The id must match the UUID pattern the contract declares, else `400` (ADR 0007)."""
    return validate_path_parameter("id", order_id, path_parameter_schema(ORDER_PATH, "id"))
