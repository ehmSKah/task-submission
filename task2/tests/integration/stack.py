"""Reaching the running stack from the tests, and the checks every reliability test ends with.

No test sleeps (ADR 0009): `eventually` waits for a state and, on timeout, fails with the last
state it observed.
"""

import os
import time
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
from confluent_kafka import (
    OFFSET_BEGINNING,
    Consumer,
    ConsumerGroupState,
    Producer,
    TopicPartition,
)
from confluent_kafka.admin import AdminClient, ConsumerGroupDescription, NewTopic
from psycopg.rows import dict_row

HOST = os.environ.get("STACK_HOST", "localhost")
KAFKA_BOOTSTRAP = f"{HOST}:{os.environ.get('KAFKA_PORT', '9094')}"
CONSUMER_DATABASE_URL = (
    "postgresql://order_consumer:local-dev-consumer"
    f"@{HOST}:{os.environ.get('POSTGRES_PORT', '5432')}/orders"
)
# librdkafka's "no partition chosen": the partitioner picks one by key.
UNASSIGNED_PARTITION = -1
STUB_URL = f"http://{HOST}:{os.environ.get('STUB_PORT', '8081')}"
FULFILMENT_TOKEN = "local-dev-token"


@dataclass(frozen=True)
class ConsumerGroup:
    """A group consuming a topic: an in-process test consumer, or the stack's own consumers."""

    topic: str
    group: str
    dead_letter_topic: str = "orders.dlt"


STACK_CONSUMERS = ConsumerGroup(topic="orders", group="order-consumer")


@dataclass(frozen=True)
class Record:
    key: bytes | None
    value: bytes | None
    headers: list[tuple[str, bytes]]
    topic: str = ""
    partition: int = 0
    offset: int = 0

    @property
    def position(self) -> str:
        """Where the record is on its topic, e.g. orders.dlt/1/42, as `replayed-from` names it."""
        return f"{self.topic}/{self.partition}/{self.offset}"

    def header(self, name: str) -> str | None:
        values = [value for key, value in self.headers if key == name]
        assert len(values) <= 1, f"header {name} appears {len(values)} times"
        return values[0].decode() if values else None


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


def publish(topic: str, events: Iterable[tuple[UUID, bytes]], partition: int | None = None) -> None:
    """Keyed by order id; `partition` puts them all on one partition, in this order."""
    publish_records(
        topic, [(str(order_id).encode(), value, []) for order_id, value in events], partition
    )


def publish_records(
    topic: str,
    records: Iterable[tuple[bytes | None, bytes | None, Sequence[tuple[str, bytes]]]],
    partition: int | None = None,
) -> None:
    """Records as they are: any key, any bytes or none, any headers."""
    producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP, "enable.idempotence": True})
    for key, value, headers in records:
        producer.produce(
            topic,
            key=key,
            value=value,
            headers=list(headers),
            partition=UNASSIGNED_PARTITION if partition is None else partition,
        )
    assert producer.flush(30) == 0, "not every record was delivered to Kafka"


def records(topic: str) -> list[Record]:
    """Every record on the topic so far, read with a throwaway group."""
    reader = Consumer(
        {
            "bootstrap.servers": KAFKA_BOOTSTRAP,
            "group.id": f"test-reader-{uuid4().hex[:12]}",
            "enable.auto.commit": False,
        }
    )
    try:
        metadata = reader.list_topics(topic, timeout=10).topics[topic]
        partitions = [TopicPartition(topic, p, OFFSET_BEGINNING) for p in metadata.partitions]
        expected = 0
        for tp in partitions:
            low, high = reader.get_watermark_offsets(tp, timeout=10)
            expected += high - low
        reader.assign(partitions)
        found: list[Record] = []
        deadline = time.monotonic() + 10
        while len(found) < expected:
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"read {len(found)} of the {expected} records on {topic}: {found!r}"
                )
            message = reader.poll(0.5)
            if message is None or message.error() is not None:
                continue
            headers = [(name, value or b"") for name, value in message.headers() or []]
            found.append(
                Record(
                    message.key(),
                    message.value(),
                    headers,
                    topic,
                    message.partition() or 0,
                    message.offset() or 0,
                )
            )
        return found
    finally:
        reader.close()


def wait_for_dead_letters(consumers: ConsumerGroup, count: int = 1) -> list[Record]:
    return eventually(
        lambda: records(consumers.dead_letter_topic),
        until=lambda found: len(found) >= count,
        what=f"{count} dead letter(s) on {consumers.dead_letter_topic}",
    )


def stored_order(order_id: UUID) -> dict[str, Any] | None:
    with psycopg.connect(CONSUMER_DATABASE_URL, row_factory=dict_row) as connection:
        return connection.execute(
            "SELECT handover_status, fulfilment_order_id, handed_over_at, placed_at, total, "
            "item_count, trace_id "
            "FROM orders WHERE order_id = %s",
            (order_id,),
        ).fetchone()


def processed_events(order_id: UUID) -> list[dict[str, Any]]:
    """The events recorded for the order, in the order they arrived (ADR 0002)."""
    with psycopg.connect(CONSUMER_DATABASE_URL, row_factory=dict_row) as connection:
        return connection.execute(
            'SELECT event_id, outcome, topic, partition, "offset", trace_id FROM processed_events '
            "WHERE order_id = %s ORDER BY received_at, event_id",
            (order_id,),
        ).fetchall()


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


def handover_requests(order_id: UUID) -> list[dict[str, Any]]:
    """The stub's journal of the order's handover requests, in arrival order (ADR 0003)."""
    response = httpx.get(
        f"{STUB_URL}/_control/requests", params={"externalReference": str(order_id)}
    )
    response.raise_for_status()
    result: list[dict[str, Any]] = response.json()
    return result


def handover_statuses(order_id: UUID) -> list[int | None]:
    """The status the stub answered each handover request for the order with, in arrival order;
    `None` while a request is still in progress."""
    return [entry["status"] for entry in handover_requests(order_id)]


def statuses_by_idempotency_key(order_id: UUID) -> list[int | None]:
    """Like `handover_statuses`, but also counting requests the stub refused before reading their
    body (a wrong token), which it journals under their `Idempotency-Key` only."""
    response = httpx.get(f"{STUB_URL}/_control/requests")
    response.raise_for_status()
    return [
        entry["status"] for entry in response.json() if entry["idempotencyKey"] == str(order_id)
    ]


def wait_until_stable(
    consumers: ConsumerGroup,
    members: int,
    replacing: Collection[str] = (),
    timeout: float = 30.0,
) -> frozenset[str]:
    """Waits until the group has settled with exactly `members` members, each holding partitions
    and none of them among the `replacing` member ids; returns the members' ids."""
    admin = AdminClient({"bootstrap.servers": KAFKA_BOOTSTRAP})

    def describe() -> ConsumerGroupDescription:
        [future] = admin.describe_consumer_groups([consumers.group]).values()
        description: ConsumerGroupDescription = future.result(timeout=10)
        return description

    settled = eventually(
        describe,
        until=lambda group: (
            group.state == ConsumerGroupState.STABLE
            and len(group.members) == members
            and all(member.assignment.topic_partitions for member in group.members)
            and not {member.member_id for member in group.members} & set(replacing)
        ),
        what=f"group {consumers.group} to settle with {members} new members holding partitions",
        timeout=timeout,
    )
    return frozenset(member.member_id for member in settled.members)


def add_fault_rule(
    order_id: UUID,
    times: int = 1,
    delay_seconds: float = 0.0,
    status: int | None = None,
    retry_after: str | None = None,
    drop_connection: bool = False,
) -> None:
    """A stub fault for this order only, so tests side by side don't interfere (ADR 0003)."""
    rule: dict[str, Any] = {
        "externalReference": str(order_id),
        "times": times,
        "delaySeconds": delay_seconds,
        "dropConnection": drop_connection,
    }
    if status is not None:
        rule["status"] = status
    if retry_after is not None:
        rule["retryAfter"] = retry_after
    response = httpx.post(f"{STUB_URL}/_control/fault-rules", json=rule)
    response.raise_for_status()


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


def assert_nothing_lost(
    order_ids: Iterable[UUID], consumers: ConsumerGroup, timeout: float = 30.0
) -> None:
    """Every order ends handed over, and the group's committed offsets reach the end (ADR 0007)."""
    for order_id in order_ids:
        wait_until_handed_over(order_id, timeout)
    assert_all_committed(consumers, timeout)


def assert_all_committed(consumers: ConsumerGroup, timeout: float = 30.0) -> None:
    eventually(
        lambda: lag(consumers),
        until=lambda lags: all(behind == 0 for behind in lags.values()),
        what=f"group {consumers.group} to commit to the end of {consumers.topic}",
        timeout=timeout,
    )


def assert_not_accepted(order_ids: Iterable[UUID]) -> None:
    """Nothing stored and nothing handed over: no order row, no fulfilment order (ADR 0001)."""
    for order_id in order_ids:
        assert order_row_count(order_id) == 0, f"order rows for {order_id}"
        assert fulfilment_orders(order_id) == [], f"fulfilment orders for {order_id}"


def wait_until_handed_over(order_id: UUID, timeout: float = 30.0) -> None:
    eventually(
        lambda: stored_order(order_id),
        until=lambda row: row is not None and row["handover_status"] == "handed_over",
        what=f"order {order_id} to be handed over",
        timeout=timeout,
    )


def assert_nothing_duplicated(order_ids: Iterable[UUID]) -> None:
    """Exactly one fulfilment order and one order row per order id (ADR 0007)."""
    for order_id in order_ids:
        assert len(fulfilment_orders(order_id)) == 1, f"fulfilment orders for {order_id}"
        assert order_row_count(order_id) == 1, f"order rows for {order_id}"
