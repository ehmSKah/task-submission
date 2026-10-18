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


class OrderStore(Protocol):
    def store_new(self, order: Order, event_id: UUID) -> bool:
        """Stores the order and its lines, awaiting handover, in one transaction.

        Returns False, writing nothing, when an order with this id is already stored.
        """

    def handover_status(self, order_id: UUID) -> HandoverStatus:
        """The handover status of a stored order."""

    def record_handover(self, order_id: UUID, fulfilment_order_id: str) -> None:
        """Marks the order as handed over to the fulfilment service."""

    def mark_handover_failed(self, order_id: UUID) -> None:
        """Marks the order's handover as given up on; a later delivery can still complete it."""


class ResponseClass(Enum):
    """What one handover attempt's answer means (ADR 0003); the class alone decides what happens
    next (ADR 0006)."""

    SUCCESS = "success"
    TRANSIENT = "transient"  # may succeed later: retried
    REJECTED = "rejected"  # this order will never be accepted as sent: dead-lettered at once
    SYSTEMIC = "systemic"  # wrong for every order: consumption stops


class HandoverFailed(Exception):
    """One handover attempt that did not end in success."""

    def __init__(
        self,
        response_class: ResponseClass,
        error: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        problem: str | None = None,
    ) -> None:
        super().__init__(error)
        self.response_class = response_class
        self.error = error  # what happened, e.g. "503 Service Unavailable" or "ReadTimeout"
        self.status = status  # None when there was no answer
        self.retry_after = retry_after  # seconds, when the answer asked for a wait
        self.problem = problem  # the start of a rejection's response body


class FulfilmentClient(Protocol):
    def hand_over(self, request: DownstreamRequest) -> HandoverResult:
        """Makes one attempt to create the fulfilment order, keyed by the request's idempotency
        key. Raises HandoverFailed, classified, for anything but a `201`."""


class Clock(Protocol):
    """Time for retry backoff (ADR 0005), so it is tested without sleeping."""

    def monotonic(self) -> float:
        """Seconds from an arbitrary start, never going backwards."""

    def sleep(self, seconds: float) -> None:
        """Returns once the time has passed."""


class DeadLetterReason(Enum):
    INVALID = "invalid"
    REJECTED = "rejected"
    RETRIES_EXHAUSTED = "retries-exhausted"


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
