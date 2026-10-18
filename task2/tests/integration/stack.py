"""Reaching the running stack from the tests, and the checks every reliability test ends with.

No test sleeps (ADR 0009): `eventually` waits for a state and, on timeout, fails with the last
state it observed.
"""

import json
import os
import time
from collections.abc import Callable, Iterable
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
from confluent_kafka import Consumer, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

HOST = os.environ.get("STACK_HOST", "localhost")
KAFKA_BOOTSTRAP = f"{HOST}:9094"
CONSUMER_DATABASE_URL = f"postgresql://order_consumer:local-dev-consumer@{HOST}:5432/orders"
STUB_URL = f"http://{HOST}:8081"
FULFILMENT_TOKEN = "local-dev-token"


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


def order_placed_event(order_id: UUID, event_id: UUID | None = None) -> bytes:
    return json.dumps(
        {
            "specversion": "1.0",
            "id": str(event_id or uuid4()),
            "source": "/orders",
            "type": "order.placed",
            "time": "2026-10-17T09:12:04Z",
            "dataschema": "urn:task2:schema:order-placed:1",
            "datacontenttype": "application/json",
            "data": {
                "orderId": str(order_id),
                "customerId": str(uuid4()),
                "placedAt": "2026-10-17T11:12:03+02:00",
                "currency": "EUR",
                "lines": [
                    {"sku": "KNE-ESC-0042", "quantity": 2, "unitPrice": 1999},
                    {"sku": "KNE-ELV-0007", "quantity": 1, "unitPrice": 500},
                ],
                "total": 4498,
            },
        }
    ).encode()


def publish(topic: str, events: Iterable[tuple[UUID, bytes]]) -> None:
    producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP, "enable.idempotence": True})
    for order_id, value in events:
        producer.produce(topic, key=str(order_id).encode(), value=value)
    assert producer.flush(30) == 0, "not every event was delivered to Kafka"


def stored_order(order_id: UUID) -> dict[str, Any] | None:
    with psycopg.connect(CONSUMER_DATABASE_URL) as connection:
        cursor = connection.execute(
            "SELECT handover_status, fulfilment_order_id, placed_at, total, item_count "
            "FROM orders WHERE order_id = %s",
            (order_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    columns = ["handover_status", "fulfilment_order_id", "placed_at", "total", "item_count"]
    return dict(zip(columns, row, strict=True))


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


def lag(topic: str, group: str) -> dict[int, int]:
    """Per partition: how many records the group has not yet committed."""
    probe = Consumer({"bootstrap.servers": KAFKA_BOOTSTRAP, "group.id": group})
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


def assert_nothing_lost(order_ids: Iterable[UUID], topic: str, group: str) -> None:
    """Every order ends handed over, and the group's committed offsets reach the end (ADR 0007)."""
    for order_id in order_ids:
        wait_until_handed_over(order_id)
    eventually(
        lambda: lag(topic, group),
        until=lambda lags: all(behind == 0 for behind in lags.values()),
        what=f"group {group} to commit to the end of {topic}",
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
