"""Rate-limit counters on the deployed counter table (ADRs 0005 and 0006).

Each count is one conditional `UpdateItem` (`ADD count 1` while the counter is absent or below
the limit, `ReturnValues=UPDATED_NEW`); these tests prove the emulator applies it as DynamoDB does.
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest

from orders_api.adapters.outbound.dynamodb_rate_limits import DynamoDbRateLimitCounters

EXPIRES_AT = datetime(2026, 10, 17, 9, 33, tzinfo=UTC)


@pytest.fixture
def counters(endpoints: dict[str, Any]) -> DynamoDbRateLimitCounters:
    return DynamoDbRateLimitCounters(endpoints["rateLimitTable"])


@pytest.fixture
def table(endpoints: dict[str, Any]) -> Any:
    return boto3.resource("dynamodb").Table(endpoints["rateLimitTable"])


def a_key() -> str:
    return f"user#adapter-test-{uuid.uuid4()}#2026-10-17T09:31Z"


def test_ttl_is_enabled_on_expires_at(endpoints: dict[str, Any]) -> None:
    description = boto3.client("dynamodb").describe_time_to_live(
        TableName=endpoints["rateLimitTable"]
    )["TimeToLiveDescription"]

    assert description["TimeToLiveStatus"] == "ENABLED"
    assert description["AttributeName"] == "expiresAt"


def test_the_first_count_creates_the_counter_with_its_expiry(
    counters: DynamoDbRateLimitCounters, table: Any
) -> None:
    key = a_key()

    assert counters.increment(key, limit=3, expires_at=EXPIRES_AT) == 1

    item = table.get_item(Key={"id": key}, ConsistentRead=True)["Item"]
    assert item == {"id": key, "count": 1, "expiresAt": int(EXPIRES_AT.timestamp())}


def test_counts_go_up_to_the_limit_and_then_stop_without_writing(
    counters: DynamoDbRateLimitCounters, table: Any
) -> None:
    key = a_key()

    counts = [counters.increment(key, limit=3, expires_at=EXPIRES_AT) for _ in range(5)]

    assert counts == [1, 2, 3, None, None]
    assert table.get_item(Key={"id": key}, ConsistentRead=True)["Item"]["count"] == 3


def test_a_limit_lowered_below_the_count_refuses_the_next_request(
    counters: DynamoDbRateLimitCounters,
) -> None:
    key = a_key()
    for _ in range(3):
        counters.increment(key, limit=10, expires_at=EXPIRES_AT)

    assert counters.increment(key, limit=2, expires_at=EXPIRES_AT) is None
    assert counters.increment(key, limit=10, expires_at=EXPIRES_AT) == 4


def test_keys_are_counted_apart(counters: DynamoDbRateLimitCounters) -> None:
    first, second = a_key(), a_key()
    counters.increment(first, limit=3, expires_at=EXPIRES_AT)

    assert counters.increment(second, limit=3, expires_at=EXPIRES_AT) == 1


@pytest.mark.parametrize("attempt", range(3))
def test_simultaneous_counts_never_exceed_the_limit(
    endpoints: dict[str, Any], table: Any, attempt: int
) -> None:
    """Twelve requests at once against a limit of five: exactly five are counted, each seeing
    its own count."""
    key = a_key()
    start = threading.Barrier(12)

    def count(_: int) -> int | None:
        own = DynamoDbRateLimitCounters(endpoints["rateLimitTable"])  # its own client
        start.wait()
        return own.increment(key, limit=5, expires_at=EXPIRES_AT)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(count, range(12)))

    assert sorted(result for result in results if result is not None) == [1, 2, 3, 4, 5]
    assert results.count(None) == 7
    assert table.get_item(Key={"id": key}, ConsistentRead=True)["Item"]["count"] == 5
