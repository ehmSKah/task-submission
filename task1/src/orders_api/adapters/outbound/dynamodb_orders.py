"""Orders in DynamoDB: one item per order, keyed by `id` (ADR 0005)."""

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import boto3
from boto3.dynamodb.conditions import ConditionBase, Key

from orders_api.application.orders import OrderPage, PagePosition
from orders_api.domain.order import LineItem, Order, OrderStatus

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

# The two list indexes (ADR 0005): each one's partition attribute, and the sort key both share.
BY_OWNER_INDEX = "byOwner"
OWNER_ATTRIBUTE = "ownerId"
BY_CREATED_INDEX = "byCreated"
LIST_ALL_ATTRIBUTE = "listAll"
LIST_ALL_PARTITION = "ORDER"
"""The one partition of `byCreated`, which every order is in."""
LIST_SORT_KEY = "createdAtId"


def list_sort_key(created_at: str, order_id: str) -> str:
    """Newest first, `id` breaking ties between equal timestamps (ADR 0005)."""
    return f"{created_at}#{order_id}"


def to_item(order: Order) -> dict[str, Any]:
    return {
        "id": order.id,
        OWNER_ATTRIBUTE: order.owner_id,
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
        LIST_SORT_KEY: list_sort_key(order.created_at, order.id),
        LIST_ALL_ATTRIBUTE: LIST_ALL_PARTITION,
    }


def from_item(item: dict[str, Any]) -> Order:
    return Order(
        id=str(item["id"]),
        owner_id=str(item[OWNER_ATTRIBUTE]),
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

    def list_newest_first(
        self, *, owner_id: str | None, limit: int, after: PagePosition | None
    ) -> OrderPage:
        """One `Query` on `byOwner` (one owner's orders) or `byCreated` (every order), newest
        first. The position is the sort key of the last order read, from `LastEvaluatedKey`.

        The next page seeks with `createdAtId < position` in the key condition rather than
        passing an `ExclusiveStartKey`: the position then stays valid after its order is deleted
        (Floci restarts from the first item when the start key's item is gone; see
        docs/verification.md), and a position can never leave the queried partition.
        """
        index, partition, value = (
            (BY_CREATED_INDEX, LIST_ALL_ATTRIBUTE, LIST_ALL_PARTITION)
            if owner_id is None
            else (BY_OWNER_INDEX, OWNER_ATTRIBUTE, owner_id)
        )
        condition: ConditionBase = Key(partition).eq(value)
        if after is not None:
            condition &= Key(LIST_SORT_KEY).lt(list_sort_key(after.created_at, after.order_id))
        response = self._table.query(
            IndexName=index, KeyConditionExpression=condition, ScanIndexForward=False, Limit=limit
        )
        last = response.get("LastEvaluatedKey")
        return OrderPage(
            orders=tuple(from_item(item) for item in response["Items"]),
            next_position=_position(str(last[LIST_SORT_KEY])) if last else None,
        )

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


def _position(sort_key: str) -> PagePosition:
    created_at, _, order_id = sort_key.partition("#")
    return PagePosition(created_at=created_at, order_id=order_id)
