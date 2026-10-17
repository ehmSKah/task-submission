"""Rate-limit counters in their own DynamoDB table: one item per scope, subject and window
(ADRs 0005 and 0006)."""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, HTTPClientError
from botocore.exceptions import ConnectionError as BotocoreConnectionError

from orders_api.application.rate_limiting import CounterStoreThrottled, CounterStoreUnavailable

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

KEY_ATTRIBUTE = "id"
COUNT_ATTRIBUTE = "count"
EXPIRY_ATTRIBUTE = "expiresAt"
"""The table's TTL attribute, in epoch seconds."""

CALL_BUDGET_SECONDS = 0.2
"""The longest one count may take on a failing store (ADR 0006)."""
CLIENT_CONFIG = Config(
    connect_timeout=0.1,
    read_timeout=0.1,
    retries={"mode": "standard", "total_max_attempts": 1},
)
"""One attempt, whose connect and read together fit the budget. A retry wouldn't: the fallback
takes its place. A count normally takes a few milliseconds."""

THROTTLING_CODES = frozenset({"ProvisionedThroughputExceededException", "ThrottlingException"})
"""A key written faster than its partition allows: almost always one client hammering it."""
UNAVAILABLE_CODES = frozenset({"RequestLimitExceeded"})
"""Throughput refused for the whole account, not one key: an outage, like a `5xx`."""


class DynamoDbRateLimitCounters:
    """A `RateLimitCounterStore`: each count is one conditional `UpdateItem`."""

    def __init__(self, table_name: str, table: "Table | None" = None) -> None:
        self._table = table or boto3.resource("dynamodb", config=CLIENT_CONFIG).Table(table_name)

    def increment(self, key: str, *, limit: int, expires_at: datetime) -> int | None:
        """`ADD count 1` on condition that the counter doesn't exist yet or is below `limit`;
        `UPDATED_NEW` returns the new count, so no read is needed. A failed condition writes
        nothing.

        Throttling raises `CounterStoreThrottled`; a timeout, a network error or a service
        error (`5xx`) raises `CounterStoreUnavailable`. Any other error is our own defect (a
        missing permission or table, a malformed request) and propagates."""
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
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return None
            if code in THROTTLING_CODES:
                raise CounterStoreThrottled(code) from error
            if code in UNAVAILABLE_CODES or _status(error) >= 500:
                raise CounterStoreUnavailable(code) from error
            raise
        except (BotocoreConnectionError, HTTPClientError) as error:
            # Timeouts, refused or dropped connections.
            raise CounterStoreUnavailable(type(error).__name__) from error
        return int(Decimal(str(response["Attributes"][COUNT_ATTRIBUTE])))


def _status(error: ClientError) -> int:
    return int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
