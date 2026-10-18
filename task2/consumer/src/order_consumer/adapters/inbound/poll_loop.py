"""The poll loop: the only place that commits offsets (ADR 0004, 0005).

One message at a time. An offset is committed synchronously, and only once its message has reached
a terminal outcome, so anything unfinished is redelivered after a crash.
"""

import logging
import threading
from collections.abc import Callable, Iterable
from typing import Any

import structlog
from confluent_kafka import Consumer, KafkaError, KafkaException, Message, TopicPartition

from order_consumer.application.processing import Delivery, OrderProcessor, Outcome
from order_consumer.config import Settings

log = structlog.get_logger()

TERMINAL = frozenset({Outcome.HANDED_OVER})


def create_consumer(settings: Settings) -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "group.id": settings.kafka_group_id,
            # Offsets follow outcomes, never the client's defaults (ADR 0004).
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
            "auto.offset.reset": "earliest",
            "partition.assignment.strategy": "cooperative-sticky",
            "logger": logging.getLogger("librdkafka"),
        }
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
    ) -> None:
        self._consumer = consumer
        self._topic = topic
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

        outcome = self._processor.process(
            Delivery(
                key=message.key(),
                value=message.value() or b"",
                topic=message.topic() or self._topic,
                partition=message.partition() or 0,
                offset=message.offset() or 0,
            )
        )
        if outcome in TERMINAL:
            self._consumer.commit(message=message, asynchronous=False)

    def _assigned(self, _consumer: Any, partitions: list[TopicPartition]) -> None:
        log.info("partitions.assigned", partitions=_partition_labels(partitions))

    def _revoked(self, _consumer: Any, partitions: list[TopicPartition]) -> None:
        log.info("partitions.revoked", partitions=_partition_labels(partitions))

    def _lost(self, _consumer: Any, partitions: list[TopicPartition]) -> None:
        log.warning("partitions.lost", partitions=_partition_labels(partitions))
