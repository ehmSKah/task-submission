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

    def add(self, order: Order) -> None:
        """One `PutItem` that refuses to replace an existing order with the same id."""
        self._table.put_item(Item=to_item(order), ConditionExpression="attribute_not_exists(id)")

    def update_status(self, current: Order, changed: Order, *, check_version: bool) -> Order | None:
        """One `UpdateItem`: set the status and `updatedAt` and increment `version`, on condition
        that the status (and the version, if asked) is still `current`'s (ADR 0005).

        A missing order fails the condition too, so the write never creates an item.
        `ALL_NEW` returns the order as stored, so no second read is needed.
        """
        condition = "#status = :expected"
        values: dict[str, Any] = {
            ":expected": current.status.value,
            ":status": changed.status.value,
            ":updatedAt": changed.updated_at,
            ":one": 1,
        }
        if check_version:
            condition += " AND #version = :version"
            values[":version"] = current.version
        try:
            response = self._table.update_item(
                Key={"id": current.id},
                UpdateExpression=(
                    "SET #status = :status, updatedAt = :updatedAt, #version = #version + :one"
                ),
                ConditionExpression=condition,
                ExpressionAttributeNames={"#status": "status", "#version": "version"},
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            return None
        return from_item(response["Attributes"])

    def delete(self, order_id: str) -> bool:
        """One `DeleteItem`, on condition that the order exists, so a miss is told apart."""
        try:
            self._table.delete_item(
                Key={"id": order_id}, ConditionExpression="attribute_exists(id)"
            )
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            return False
        return True
