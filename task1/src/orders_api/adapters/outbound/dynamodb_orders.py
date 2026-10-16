"""Orders in DynamoDB: one item per order, keyed by `id` (ADR 0005)."""

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import boto3

from orders_api.domain.order import LineItem, Order, OrderStatus

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

LIST_ALL_PARTITION = "ORDER"


def to_item(order: Order) -> dict[str, Any]:
    return {
        "id": order.id,
        "ownerId": order.owner_id,
        "status": order.status.value,
        "currency": order.currency,
        "items": [
            {"sku": item.sku, "quantity": item.quantity, "unitPrice": item.unit_price}
            for item in order.items
        ],
        "createdAt": order.created_at,
        "updatedAt": order.updated_at,
        "version": order.version,
        # Sort key of both list indexes: newest first, `id` breaks ties (ADR 0005).
        "createdAtId": f"{order.created_at}#{order.id}",
        "listAll": LIST_ALL_PARTITION,
    }


def from_item(item: dict[str, Any]) -> Order:
    return Order(
        id=str(item["id"]),
        owner_id=str(item["ownerId"]),
        status=OrderStatus(item["status"]),
        currency=str(item["currency"]),
        items=tuple(
            LineItem(
                sku=str(line["sku"]),
                quantity=int(line["quantity"]),
                unit_price=int(line["unitPrice"]),
            )
            for line in item["items"]
        ),
        created_at=str(item["createdAt"]),
        updated_at=str(item["updatedAt"]),
        version=int(Decimal(item["version"])),
    )


class DynamoDbOrderRepository:
    def __init__(self, table_name: str, table: "Table | None" = None) -> None:
        self._table = table or boto3.resource("dynamodb").Table(table_name)

    def get(self, order_id: str) -> Order | None:
        response = self._table.get_item(Key={"id": order_id}, ConsistentRead=True)
        item = response.get("Item")
        return from_item(item) if item else None
