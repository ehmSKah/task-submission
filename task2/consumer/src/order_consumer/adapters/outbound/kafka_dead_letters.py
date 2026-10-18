"""The dead-letter topic, written by an idempotent Kafka producer (ADR 0006).

A dead letter is the original record, unchanged, plus `dlt.*` headers saying what went wrong, and
the trace's traceparent when the consumer started the trace. `write` returns only once the broker
has confirmed it, so the poll loop can commit after it.
"""

import logging
from datetime import UTC, datetime

import structlog
from confluent_kafka import KafkaError, KafkaException, Message, Producer

from order_consumer.application.ports import DeadLetter, DeadLetterUnwritable
from order_consumer.config import Settings
from order_consumer.tracing import TRACEPARENT, TRACESTATE

log = structlog.get_logger()

# librdkafka's default is 5 minutes; a dead letter must be confirmed well within the poll budget.
DELIVERY_TIMEOUT_MS = 10_000
# Waiting a little longer than the delivery timeout lets librdkafka report its own verdict.
FLUSH_TIMEOUT_S = DELIVERY_TIMEOUT_MS / 1000 + 5


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


def _original_headers(dead_letter: DeadLetter) -> list[tuple[str, bytes | None]]:
    """Copied unchanged, unless the consumer started the trace: then its traceparent replaces the
    record's, and any tracestate, which belonged to the replaced trace, is left out (ADR 0008)."""
    headers = list(dead_letter.delivery.headers)
    traceparent = dead_letter.traceparent
    if traceparent is None:
        return headers
    replaced = (TRACEPARENT, TRACESTATE)
    kept = [(name, value) for name, value in headers if name.lower() not in replaced]
    return [*kept, (TRACEPARENT, traceparent.encode())]


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class KafkaDeadLetterSink:
    def __init__(self, producer: Producer, topic: str, consumer: str) -> None:
        self._producer = producer
        self._topic = topic
        self._consumer = consumer  # consumer group and instance, e.g. order-consumer/<host name>

    def write(self, dead_letter: DeadLetter) -> None:
        delivery = dead_letter.delivery
        failure: dict[str, str | bytes] = {
            "dlt.reason": dead_letter.reason.value,
            "dlt.detail": dead_letter.detail.encode(),
            "dlt.attempts": str(dead_letter.attempts),
            "dlt.source": f"{delivery.topic}/{delivery.partition}/{delivery.offset}",
            "dlt.failed-at": _utc(datetime.now(UTC)),
            "dlt.consumer": self._consumer,
        }
        if delivery.timestamp is not None:
            failure["dlt.source-timestamp"] = _utc(delivery.timestamp)
        if dead_letter.event_id is not None:
            failure["dlt.event-id"] = str(dead_letter.event_id)
        if dead_letter.order_id is not None:
            failure["dlt.order-id"] = str(dead_letter.order_id)

        failures: list[str] = []
        confirmed: list[bool] = []

        def delivered(error: KafkaError | None, _message: Message) -> None:
            if error is None:
                confirmed.append(True)
            else:
                failures.append(error.str())

        try:
            self._producer.produce(
                self._topic,
                key=delivery.key,
                value=delivery.value,
                headers=[*_original_headers(dead_letter), *failure.items()],
                on_delivery=delivered,
            )
            self._producer.flush(FLUSH_TIMEOUT_S)
        except (BufferError, KafkaException) as error:  # e.g. a full queue, a record too large
            failures.append(str(error))
        if not confirmed:
            reason = failures[0] if failures else "not confirmed in time"
            log.error("dead_letter.write_failed", reason=reason, topic=self._topic)
            raise DeadLetterUnwritable(reason)

    def close(self) -> None:
        self._producer.flush(FLUSH_TIMEOUT_S)
