"""The poll loop: the only place that commits offsets (ADR 0004, 0005).

One message at a time. An offset is committed synchronously, and only once its message has reached
a terminal outcome, so anything unfinished is redelivered after a crash. Each message is processed
in a span of its own, continuing the trace its record carries (ADR 0008).
"""

import logging
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

import structlog
from confluent_kafka import (
    TIMESTAMP_NOT_AVAILABLE,
    Consumer,
    KafkaError,
    KafkaException,
    Message,
    TopicPartition,
)
from opentelemetry import trace

from order_consumer import tracing
from order_consumer.application.ports import Delivery, MessageTrace
from order_consumer.application.processing import OrderProcessor, Outcome
from order_consumer.config import Settings

log = structlog.get_logger()
tracer = trace.get_tracer(__name__)

TERMINAL = frozenset({Outcome.HANDED_OVER, Outcome.DUPLICATE, Outcome.DEAD_LETTERED})


def create_consumer(settings: Settings) -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "group.id": settings.kafka_group_id,
            # Offsets follow outcomes, never the client's defaults (ADR 0004).
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
            "auto.offset.reset": "earliest",
            # A handover's retries keep the message in hand without polling; the start-up check
            # keeps them within half of this (ADR 0006).
            "max.poll.interval.ms": settings.kafka_max_poll_interval_ms,
            "partition.assignment.strategy": "cooperative-sticky",
            "logger": logging.getLogger("librdkafka"),
        }
    )


def _delivery(message: Message, topic: str, message_trace: MessageTrace) -> Delivery:
    timestamp_type, timestamp_ms = message.timestamp()
    return Delivery(
        key=message.key(),
        value=message.value(),
        topic=topic,
        partition=message.partition() or 0,
        offset=message.offset() or 0,
        headers=_headers(message),
        timestamp=None
        if timestamp_type == TIMESTAMP_NOT_AVAILABLE
        else datetime.fromtimestamp(timestamp_ms / 1000, UTC),
        trace=message_trace,
    )


def _headers(message: Message) -> tuple[tuple[str, bytes | None], ...]:
    """Kept in their order and with repeated keys. The client's types also allow str values,
    though a consumed record's are bytes."""
    headers = message.headers() or []
    pairs = headers.items() if isinstance(headers, dict) else headers
    return tuple(
        (name, value.encode() if isinstance(value, str) else value) for name, value in pairs
    )


def _partition_labels(partitions: Iterable[TopicPartition]) -> list[str]:
    return [f"{partition.topic}/{partition.partition}" for partition in partitions]


class PollLoop:
    def __init__(
        self,
        consumer: Consumer,
        topic: str,
        processor: OrderProcessor,
        stop: threading.Event,
        on_close: Iterable[Callable[[], None]] = (),
        *,
        group: str,
    ) -> None:
        self._consumer = consumer
        self._topic = topic
        self._group = group
        self._processor = processor
        self._stop = stop
        self._on_close = list(on_close)

    def run(self) -> None:
        self._consumer.subscribe(
            [self._topic],
            on_assign=self._assigned,
            on_revoke=self._revoked,
            on_lost=self._lost,
        )
        log.info("consumer.started", topic=self._topic)
        try:
            while not self._stop.is_set():
                message = self._consumer.poll(1.0)
                if message is not None:
                    self._handle(message)
        finally:
            self._consumer.close()
            for close in self._on_close:
                close()

    def _handle(self, message: Message) -> None:
        error = message.error()
        if error is not None:
            if error.fatal():
                raise KafkaException(error)
            if error.code() != KafkaError._PARTITION_EOF:
                log.warning("kafka.error", reason=error.str())
            return

        outcome = self._process(message)
        if outcome in TERMINAL:
            self._consumer.commit(message=message, asynchronous=False)

    def _process(self, message: Message) -> Outcome:
        """In the message's span, named and described by OpenTelemetry's messaging conventions."""
        topic = message.topic() or self._topic
        parent, source = tracing.incoming_trace(_headers(message))
        with tracer.start_as_current_span(
            f"process {topic}",
            context=parent,
            kind=trace.SpanKind.CONSUMER,
            attributes={
                "messaging.system": "kafka",
                "messaging.operation.type": "process",
                "messaging.operation.name": "process",
                "messaging.destination.name": topic,
                "messaging.destination.partition.id": str(message.partition() or 0),
                "messaging.kafka.offset": message.offset() or 0,
                "messaging.consumer.group.name": self._group,
            },
        ) as span:
            outcome = self._processor.process(
                _delivery(message, topic, tracing.current_trace(source))
            )
            span.set_attribute("outcome", outcome.value)
            return outcome

    def _assigned(self, _consumer: Any, partitions: list[TopicPartition]) -> None:
        log.info("partitions.assigned", partitions=_partition_labels(partitions))

    def _revoked(self, _consumer: Any, partitions: list[TopicPartition]) -> None:
        log.info("partitions.revoked", partitions=_partition_labels(partitions))

    def _lost(self, _consumer: Any, partitions: list[TopicPartition]) -> None:
        log.warning("partitions.lost", partitions=_partition_labels(partitions))
