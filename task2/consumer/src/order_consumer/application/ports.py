"""The ports the processing use case depends on (ADR 0005)."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol
from uuid import UUID

from order_consumer.domain.downstream import DownstreamRequest
from order_consumer.domain.order import Order


@dataclass(frozen=True, slots=True)
class Delivery:
    """One Kafka record, without anything Kafka-specific."""

    key: bytes | None
    value: bytes
    topic: str
    partition: int
    offset: int
    headers: tuple[tuple[str, bytes | None], ...] = ()
    timestamp: datetime | None = None  # the record's time, UTC


@dataclass(frozen=True, slots=True)
class HandoverResult:
    fulfilment_order_id: str


class OrderStore(Protocol):
    def store_new(self, order: Order, event_id: UUID) -> None:
        """Stores the order and its lines, awaiting handover, in one transaction."""

    def record_handover(self, order_id: UUID, fulfilment_order_id: str) -> None:
        """Marks the order as handed over to the fulfilment service."""


class FulfilmentClient(Protocol):
    def hand_over(self, request: DownstreamRequest) -> HandoverResult:
        """Creates the fulfilment order, keyed by the request's idempotency key."""


class DeadLetterReason(Enum):
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """A delivery set aside unchanged, with what went wrong (ADR 0006)."""

    delivery: Delivery
    reason: DeadLetterReason
    detail: str  # JSON, at most 4 KB in UTF-8
    attempts: int  # handover attempts made
    event_id: UUID | None  # when it could be read
    order_id: UUID | None  # when it could be read


class DeadLetterSink(Protocol):
    def write(self, dead_letter: DeadLetter) -> None:
        """Returns once the dead letter is durably written; raises if it can't be."""
