"""The dead-letter topic, written by an idempotent Kafka producer (ADR 0006).

A dead letter is the original record, unchanged, plus `dlt.*` headers saying what went wrong.
`write` returns only once the broker has confirmed it, so the poll loop can commit after it.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from confluent_kafka import KafkaError, Message, Producer

from order_consumer.application.ports import DeadLetter
from order_consumer.config import Settings

log = structlog.get_logger()

# librdkafka's default is 5 minutes; a dead letter must be confirmed well within the poll budget.
DELIVERY_TIMEOUT_MS = 10_000


class DeadLetterWriteFailed(Exception):
    pass


def create_producer(settings: Settings) -> Producer:
    return Producer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            # acks=all, and the producer's own retries neither duplicate nor reorder.
            "enable.idempotence": True,
            "delivery.timeout.ms": DELIVERY_TIMEOUT_MS,
            "linger.ms": 0,
            "logger": logging.getLogger("librdkafka"),
        }
    )


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class KafkaDeadLetterSink:
    def __init__(
        self,
        producer: Producer,
        topic: str,
        consumer: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._producer = producer
        self._topic = topic
        self._consumer = consumer  # consumer group and instance
        self._now = now

    def write(self, dead_letter: DeadLetter) -> None:
        delivery = dead_letter.delivery
        failure: dict[str, str | bytes] = {
            "dlt.reason": dead_letter.reason.value,
            "dlt.detail": dead_letter.detail.encode(),
            "dlt.attempts": str(dead_letter.attempts),
            "dlt.source": f"{delivery.topic}/{delivery.partition}/{delivery.offset}",
            "dlt.failed-at": _utc(self._now()),
            "dlt.consumer": self._consumer,
        }
        if delivery.timestamp is not None:
            failure["dlt.source-timestamp"] = _utc(delivery.timestamp)
        if dead_letter.event_id is not None:
            failure["dlt.event-id"] = str(dead_letter.event_id)
        if dead_letter.order_id is not None:
            failure["dlt.order-id"] = str(dead_letter.order_id)

        outcome: list[KafkaError | None] = []

        def delivered(error: KafkaError | None, _message: Message) -> None:
            outcome.append(error)

        self._producer.produce(
            self._topic,
            key=delivery.key,
            value=delivery.value,
            headers=[*delivery.headers, *failure.items()],
            on_delivery=delivered,
        )
        self._producer.flush(DELIVERY_TIMEOUT_MS / 1000 + 5)
        if outcome != [None]:
            reason = "not confirmed in time" if not outcome else str(outcome[0])
            log.error("dead_letter.write_failed", reason=reason, topic=self._topic)
            raise DeadLetterWriteFailed(reason)

    def close(self) -> None:
        self._producer.flush(DELIVERY_TIMEOUT_MS / 1000)
