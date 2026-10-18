"""Republishing replayed dead letters to the order topic, with an idempotent producer (ADR 0006).

Each record goes back with the dead letter's key, so it lands on its order's partition, and in the
order given, so the dead letters of one order are replayed in the order they were written.
"""

import logging
from collections.abc import Callable

from confluent_kafka import KafkaError, KafkaException, Message, Producer

from order_consumer.adapters.outbound.kafka_dead_letters import (
    DELIVERY_TIMEOUT_MS,
    FLUSH_TIMEOUT_S,
)
from order_consumer.application.replay import DeadLetterRecord, Headers


class KafkaRepublisher:
    def __init__(self, bootstrap_servers: str, topic: str) -> None:
        self._topic = topic
        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap_servers,
                "enable.idempotence": True,
                "delivery.timeout.ms": DELIVERY_TIMEOUT_MS,
                "linger.ms": 0,
                "logger": logging.getLogger("librdkafka"),
            }
        )

    def republish(self, replays: list[tuple[DeadLetterRecord, Headers]]) -> dict[str, str]:
        """Publishes each dead letter's original record with the headers given and waits for the
        broker's confirmations. Returns why, per dead letter position, a record was not
        confirmed."""
        failed: dict[str, str] = {}
        unconfirmed = {dead_letter.position for dead_letter, _ in replays}

        def delivered_for(position: str) -> Callable[[KafkaError | None, Message], None]:
            def delivered(error: KafkaError | None, _message: Message) -> None:
                unconfirmed.discard(position)
                if error is not None:
                    failed[position] = error.str()

            return delivered

        for dead_letter, headers in replays:
            try:
                self._producer.produce(
                    self._topic,
                    key=dead_letter.key,
                    value=dead_letter.value,
                    headers=list(headers),
                    on_delivery=delivered_for(dead_letter.position),
                )
            except (BufferError, KafkaException) as error:
                unconfirmed.discard(dead_letter.position)
                failed[dead_letter.position] = str(error)
            self._producer.poll(0)  # serves delivery reports, so the queue never fills
        self._producer.flush(FLUSH_TIMEOUT_S)
        return failed | {position: "not confirmed in time" for position in unconfirmed}
