"""Reaching the running stack from the tests, and the checks every reliability test ends with.

No test sleeps (ADR 0009): `eventually` waits for a state and, on timeout, fails with the last
state it observed.
"""

import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import psycopg
from confluent_kafka import Consumer, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic
from psycopg.rows import dict_row

HOST = os.environ.get("STACK_HOST", "localhost")
KAFKA_BOOTSTRAP = f"{HOST}:{os.environ.get('KAFKA_PORT', '9094')}"
CONSUMER_DATABASE_URL = (
    "postgresql://order_consumer:local-dev-consumer"
    f"@{HOST}:{os.environ.get('POSTGRES_PORT', '5432')}/orders"
)
STUB_URL = f"http://{HOST}:{os.environ.get('STUB_PORT', '8081')}"
FULFILMENT_TOKEN = "local-dev-token"


@dataclass(frozen=True)
class ConsumerGroup:
    """A group consuming a topic: an in-process test consumer, or the stack's own consumers."""

    topic: str
    group: str


STACK_CONSUMERS = ConsumerGroup(topic="orders", group="order-consumer")


def eventually[T](
    probe: Callable[[], T], until: Callable[[T], bool], what: str, timeout: float = 30.0
) -> T:
    deadline = time.monotonic() + timeout
    while True:
        observed = probe()
        if until(observed):
            return observed
        if time.monotonic() > deadline:
            raise AssertionError(
                f"timed out after {timeout:.0f} s waiting for {what}; last observed: {observed!r}"
            )
        time.sleep(0.1)


def create_topic(name: str, partitions: int = 3) -> None:
    admin = AdminClient({"bootstrap.servers": KAFKA_BOOTSTRAP})
    for future in admin.create_topics([NewTopic(name, partitions, 1)]).values():
        future.result(timeout=30)


def publish(topic: str, events: Iterable[tuple[UUID, bytes]]) -> None:
    producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP, "enable.idempotence": True})
    for order_id, value in events:
        producer.produce(topic, key=str(order_id).encode(), value=value)
    assert producer.flush(30) == 0, "not every event was delivered to Kafka"


def stored_order(order_id: UUID) -> dict[str, Any] | None:
    with psycopg.connect(CONSUMER_DATABASE_URL, row_factory=dict_row) as connection:
        return connection.execute(
            "SELECT handover_status, fulfilment_order_id, placed_at, total, item_count "
            "FROM orders WHERE order_id = %s",
            (order_id,),
        ).fetchone()


def order_row_count(order_id: UUID) -> int:
    with psycopg.connect(CONSUMER_DATABASE_URL) as connection:
        row = connection.execute(
            "SELECT count(*) FROM orders WHERE order_id = %s", (order_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def fulfilment_orders(order_id: UUID) -> list[dict[str, str]]:
    response = httpx.get(
        f"{STUB_URL}/_control/fulfilment-orders", params={"externalReference": str(order_id)}
    )
    response.raise_for_status()
    result: list[dict[str, str]] = response.json()
    return result


def handover_requests(order_id: UUID) -> list[int | None]:
    """The status the stub answered each handover request for the order with, in arrival order;
    `None` while a request is still in progress."""
    response = httpx.get(
        f"{STUB_URL}/_control/requests", params={"externalReference": str(order_id)}
    )
    response.raise_for_status()
    return [entry["status"] for entry in response.json()]


def lag(consumers: ConsumerGroup) -> dict[int, int]:
    """Per partition: how many records the group has not yet committed."""
    topic = consumers.topic
    probe = Consumer({"bootstrap.servers": KAFKA_BOOTSTRAP, "group.id": consumers.group})
    try:
        metadata = probe.list_topics(topic, timeout=10).topics[topic]
        partitions = [TopicPartition(topic, partition) for partition in metadata.partitions]
        committed = {tp.partition: tp.offset for tp in probe.committed(partitions, timeout=10)}
        lags = {}
        for tp in partitions:
            _low, high = probe.get_watermark_offsets(tp, timeout=10)
            position = committed[tp.partition]
            lags[tp.partition] = high - (position if position >= 0 else 0)
        return lags
    finally:
        probe.close()


def assert_nothing_lost(order_ids: Iterable[UUID], consumers: ConsumerGroup) -> None:
    """Every order ends handed over, and the group's committed offsets reach the end (ADR 0007)."""
    for order_id in order_ids:
        wait_until_handed_over(order_id)
    eventually(
        lambda: lag(consumers),
        until=lambda lags: all(behind == 0 for behind in lags.values()),
        what=f"group {consumers.group} to commit to the end of {consumers.topic}",
    )


def wait_until_handed_over(order_id: UUID) -> None:
    eventually(
        lambda: stored_order(order_id),
        until=lambda row: row is not None and row["handover_status"] == "handed_over",
        what=f"order {order_id} to be handed over",
    )


def assert_nothing_duplicated(order_ids: Iterable[UUID]) -> None:
    """Exactly one fulfilment order and one order row per order id (ADR 0007)."""
    for order_id in order_ids:
        assert len(fulfilment_orders(order_id)) == 1, f"fulfilment orders for {order_id}"
        assert order_row_count(order_id) == 1, f"order rows for {order_id}"
