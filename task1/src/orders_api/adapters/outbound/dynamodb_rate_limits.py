"""Rate-limit counters in their own DynamoDB table: one item per scope, subject and window
(ADRs 0005 and 0006)."""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import boto3

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

KEY_ATTRIBUTE = "id"
COUNT_ATTRIBUTE = "count"
EXPIRY_ATTRIBUTE = "expiresAt"
"""The table's TTL attribute, in epoch seconds."""


class DynamoDbRateLimitCounters:
    """A `RateLimitCounterStore`: each count is one conditional `UpdateItem`."""

    def __init__(self, table_name: str, table: "Table | None" = None) -> None:
        self._table = table or boto3.resource("dynamodb").Table(table_name)

    def increment(self, key: str, *, limit: int, expires_at: datetime) -> int | None:
        """`ADD count 1` on condition that the counter doesn't exist yet or is below `limit`;
        `UPDATED_NEW` returns the new count, so no read is needed. A failed condition writes
        nothing. Other errors (throttling, timeouts) propagate."""
        try:
            response = self._table.update_item(
                Key={KEY_ATTRIBUTE: key},
                UpdateExpression="ADD #count :one SET #expiresAt = :expiresAt",
                ConditionExpression="attribute_not_exists(#count) OR #count < :limit",
                ExpressionAttributeNames={
                    "#count": COUNT_ATTRIBUTE,
                    "#expiresAt": EXPIRY_ATTRIBUTE,
                },
                ExpressionAttributeValues={
                    ":one": 1,
                    ":limit": limit,
                    ":expiresAt": int(expires_at.timestamp()),
                },
                ReturnValues="UPDATED_NEW",
            )
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            return None
        return int(Decimal(str(response["Attributes"][COUNT_ATTRIBUTE])))
