"""Reading the dead-letter topic for a replay (ADR 0006).

Every dead letter on the topic when the replay starts, in each partition's order. The replay's own
consumer group never commits: a dead letter stays a work item until its retention ends, and the
filters, not offsets, choose what a replay takes.
"""

import logging
import time
from datetime import UTC, datetime

from confluent_kafka import (
    OFFSET_BEGINNING,
    TIMESTAMP_NOT_AVAILABLE,
    Consumer,
    KafkaError,
    KafkaException,
    Message,
    TopicPartition,
)

from order_consumer.application.replay import DeadLetterRecord

# Reading a whole partition is quick; this only bounds a broker that stops answering.
READ_TIMEOUT_SECONDS = 60.0
METADATA_TIMEOUT_SECONDS = 10.0


class DeadLettersUnreadable(Exception):
    """The dead-letter topic could not be read to its end."""


def _record(message: Message) -> DeadLetterRecord:
    timestamp_type, timestamp_ms = message.timestamp()
    headers = message.headers() or []
    pairs = headers.items() if isinstance(headers, dict) else headers
    return DeadLetterRecord(
        topic=message.topic() or "",
        partition=message.partition() or 0,
        offset=message.offset() or 0,
        key=message.key(),
        value=message.value(),
        headers=tuple(
            (name, value.encode() if isinstance(value, str) else value) for name, value in pairs
        ),
        timestamp=None
        if timestamp_type == TIMESTAMP_NOT_AVAILABLE
        else datetime.fromtimestamp(timestamp_ms / 1000, UTC),
    )


def read_dead_letters(bootstrap_servers: str, topic: str, group: str) -> list[DeadLetterRecord]:
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap_servers,
            "group.id": group,
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
            "enable.partition.eof": True,
            "logger": logging.getLogger("librdkafka"),
        }
    )
    try:
        metadata = consumer.list_topics(topic, timeout=METADATA_TIMEOUT_SECONDS).topics[topic]
        if metadata.error is not None:
            raise DeadLettersUnreadable(f"{topic}: {metadata.error.str()}")
        # Where each partition ends now: dead letters written during the replay wait for the next.
        ends: dict[int, int] = {}
        for partition in metadata.partitions:
            low, high = consumer.get_watermark_offsets(
                TopicPartition(topic, partition), timeout=METADATA_TIMEOUT_SECONDS
            )
            if high > low:
                ends[partition] = high
        consumer.assign([TopicPartition(topic, p, OFFSET_BEGINNING) for p in ends])
        found: list[DeadLetterRecord] = []
        deadline = time.monotonic() + READ_TIMEOUT_SECONDS
        while ends:
            if time.monotonic() > deadline:
                raise DeadLettersUnreadable(
                    f"{topic}: partitions {sorted(ends)} not read to their end in time"
                )
            message = consumer.poll(1.0)
            if message is None:
                continue
            error = message.error()
            partition, offset = message.partition() or 0, message.offset() or 0
            if error is not None:
                if error.fatal():
                    raise KafkaException(error)
                if error.code() == KafkaError._PARTITION_EOF:
                    ends.pop(partition, None)
                continue
            if partition in ends and offset < ends[partition]:
                found.append(_record(message))
                if offset + 1 >= ends[partition]:
                    del ends[partition]
        return sorted(found, key=lambda record: (record.partition, record.offset))
    finally:
        consumer.close()
