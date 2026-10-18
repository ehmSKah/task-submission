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
    value: bytes | None  # None for a tombstone
    topic: str
    partition: int
    offset: int
    headers: tuple[tuple[str, bytes | None], ...] = ()
    timestamp: datetime | None = None  # the record's time, UTC


@dataclass(frozen=True, slots=True)
class HandoverResult:
    fulfilment_order_id: str


class HandoverStatus(Enum):
    """How far a stored order's handover got (ADR 0004). Anything but handed over is unfinished."""

    AWAITING_HANDOVER = "awaiting_handover"
    HANDED_OVER = "handed_over"
    HANDOVER_FAILED = "handover_failed"


@dataclass(frozen=True, slots=True)
class ReceivedEvent:
    """An event id and where the event was read: what `processed_events` records (ADR 0002)."""

    event_id: UUID
    topic: str
    partition: int
    offset: int


@dataclass(frozen=True, slots=True)
class Stored:
    """The order was new: it is now stored, awaiting handover, and the event recorded."""


@dataclass(frozen=True, slots=True)
class AlreadyStored:
    """An order with this id was already stored: its first accepted version."""

    order: Order
    first_event_id: UUID
    handover_status: HandoverStatus
    event_recorded: bool  # the incoming event id is already recorded for this order


@dataclass(frozen=True, slots=True)
class EventIdTaken:
    """The incoming event id is already recorded for another order: nothing was stored, whether
    or not an order with the incoming id exists."""


class OrderStore(Protocol):
    def store_new(
        self, order: Order, event: ReceivedEvent
    ) -> Stored | AlreadyStored | EventIdTaken:
        """Stores the order, its lines and the event as accepted, in one transaction.

        Writes nothing when an order with this id is already stored, or when the event id is
        already recorded for another order. Of two consumers racing on one order, one stores it
        and the other gets the stored version.
        """

    def record_duplicate(self, order_id: UUID, event: ReceivedEvent) -> None:
        """Records the event as a duplicate of the order, unless its id is already recorded."""

    def record_handover(self, order_id: UUID, fulfilment_order_id: str) -> None:
        """Marks the order as handed over to the fulfilment service."""


class FulfilmentClient(Protocol):
    def hand_over(self, request: DownstreamRequest) -> HandoverResult:
        """Creates the fulfilment order, keyed by the request's idempotency key."""


class DeadLetterReason(Enum):
    INVALID = "invalid"
    CONFLICT = "conflict"


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
