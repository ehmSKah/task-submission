"""Processing one delivery: read it, then store, hand over and record (ADR 0004), or set an invalid
event aside as a dead letter (ADR 0006).

Returns an Outcome and never commits: only the poll loop commits, according to the outcome
(ADR 0005).
"""

import json
from enum import Enum

import structlog

from order_consumer.application.ports import (
    DeadLetter,
    DeadLetterReason,
    DeadLetterSink,
    Delivery,
    FulfilmentClient,
    OrderStore,
)
from order_consumer.domain.downstream import to_downstream_request
from order_consumer.domain.event import OrderPlacedReader
from order_consumer.domain.order import to_order
from order_consumer.domain.validation import InvalidEvent

log = structlog.get_logger()

MAX_DETAIL_BYTES = 4096


class Outcome(Enum):
    HANDED_OVER = "handed_over"
    DEAD_LETTERED = "dead_lettered"


class OrderProcessor:
    def __init__(
        self,
        store: OrderStore,
        fulfilment: FulfilmentClient,
        dead_letters: DeadLetterSink,
        reader: OrderPlacedReader,
    ) -> None:
        self._store = store
        self._fulfilment = fulfilment
        self._dead_letters = dead_letters
        self._reader = reader

    def process(self, delivery: Delivery) -> Outcome:
        with structlog.contextvars.bound_contextvars(
            topic=delivery.topic, partition=delivery.partition, offset=delivery.offset
        ):
            try:
                event = self._reader.read(delivery.key, delivery.value)
            except InvalidEvent as invalid:
                return self._set_aside(delivery, invalid)

            order = to_order(event.data)
            with structlog.contextvars.bound_contextvars(
                event_id=str(event.event_id), order_id=str(order.order_id)
            ):
                log.info("event.received")

                self._store.store_new(order, event.event_id)
                log.info("order.accepted")

                result = self._fulfilment.hand_over(to_downstream_request(order))
                self._store.record_handover(order.order_id, result.fulfilment_order_id)
                log.info("handover.completed", fulfilment_order_id=result.fulfilment_order_id)
                return Outcome.HANDED_OVER

    def _set_aside(self, delivery: Delivery, invalid: InvalidEvent) -> Outcome:
        """Invalid is permanent (ADR 0001): dead-lettered at once, never retried, nothing
        stored. A failure to write the dead letter propagates, so nothing is committed."""
        readable = {
            name: str(value)
            for name, value in (("event_id", invalid.event_id), ("order_id", invalid.order_id))
            if value is not None
        }
        with structlog.contextvars.bound_contextvars(**readable):
            log.info("event.received")
            log.warning("event.invalid", layer=invalid.layer.value, error_count=invalid.error_count)
            self._dead_letters.write(
                DeadLetter(
                    delivery=delivery,
                    reason=DeadLetterReason.INVALID,
                    detail=_invalid_detail(invalid),
                    attempts=0,
                    event_id=invalid.event_id,
                    order_id=invalid.order_id,
                )
            )
            log.warning("dead_letter.written", reason=DeadLetterReason.INVALID.value)
            return Outcome.DEAD_LETTERED


def _invalid_detail(invalid: InvalidEvent) -> str:
    """The layer and its errors; errors are left off the end until the detail fits in 4 KB, while
    the count still says how many there were."""
    errors = [{"pointer": error.pointer, "message": error.message} for error in invalid.errors]
    while True:
        detail = json.dumps(
            {"layer": invalid.layer.value, "errorCount": invalid.error_count, "errors": errors},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(detail.encode()) <= MAX_DETAIL_BYTES or not errors:
            return detail
        errors.pop()
