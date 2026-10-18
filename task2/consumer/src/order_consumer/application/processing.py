"""Processing one delivery: store, hand over, record (ADR 0004).

Returns an Outcome and never commits: only the poll loop commits, according to the outcome
(ADR 0005).
"""

from dataclasses import dataclass
from enum import Enum

import structlog

from order_consumer.application.ports import FulfilmentClient, OrderStore
from order_consumer.domain.downstream import to_downstream_request
from order_consumer.domain.event import parse_order_placed
from order_consumer.domain.order import to_order

log = structlog.get_logger()


class Outcome(Enum):
    HANDED_OVER = "handed_over"


@dataclass(frozen=True, slots=True)
class Delivery:
    """One Kafka record, without anything Kafka-specific."""

    key: bytes | None
    value: bytes
    topic: str
    partition: int
    offset: int


class OrderProcessor:
    def __init__(self, store: OrderStore, fulfilment: FulfilmentClient) -> None:
        self._store = store
        self._fulfilment = fulfilment

    def process(self, delivery: Delivery) -> Outcome:
        with structlog.contextvars.bound_contextvars(
            topic=delivery.topic, partition=delivery.partition, offset=delivery.offset
        ):
            event = parse_order_placed(delivery.value)
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
