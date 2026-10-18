"""The ports the processing use case depends on (ADR 0005)."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol
from uuid import UUID

from order_consumer.domain.downstream import DownstreamRequest
from order_consumer.domain.order import Order


class TraceSource(Enum):
    """Where a message's trace came from (ADR 0008)."""

    HEADER = "header"  # the record's traceparent, continued
    NEW = "new"  # the record had none
    REPLACED = "replaced"  # the record's was invalid, and is never logged as it is


@dataclass(frozen=True, slots=True)
class MessageTrace:
    """The trace a delivery is processed in: its trace id is the correlation id (ADR 0008)."""

    trace_id: str  # 32 lower-case hex digits
    traceparent: str  # the W3C header naming the message's own span
    source: TraceSource

    @property
    def started_here(self) -> bool:
        """The consumer started the trace, so the record's headers don't carry it."""
        return self.source is not TraceSource.HEADER


@dataclass(frozen=True, slots=True)
class Delivery:
    """One Kafka record, without anything Kafka-specific, and the trace it is processed in."""

    key: bytes | None
    value: bytes | None  # None for a tombstone
    topic: str
    partition: int
    offset: int
    headers: tuple[tuple[str, bytes | None], ...] = ()
    timestamp: datetime | None = None  # the record's time, UTC
    trace: MessageTrace = field(kw_only=True)


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
    """An event id, where the event was read and the trace it was processed in: what
    `processed_events` records (ADR 0002, 0008)."""

    event_id: UUID
    topic: str
    partition: int
    offset: int
    trace_id: str


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


class StoreUnavailable(Exception):
    """The database could not be reached, even after reconnecting (ADR 0006)."""


class OrderStore(Protocol):
    """Every step raises StoreUnavailable when the database can't be reached. Each is safe to
    repeat, so a step whose outcome is unknown is simply done again."""

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
    """Time for the poll loop and the flood guard (ADR 0005), so it is tested without waiting."""

    def monotonic(self) -> float:
        """Seconds from an arbitrary start, never going backwards."""


class StopSignal(Protocol):
    """Shutdown requested (ADR 0007): the flag the signal handler sets, a `threading.Event`.
    Retry backoff waits on it, so a stop cuts a wait short."""

    def is_set(self) -> bool:
        """Whether a stop has been requested."""

    def wait(self, timeout: float) -> bool:
        """Returns once the time has passed, or earlier once a stop is requested; True then."""


class DeadLetterReason(Enum):
    INVALID = "invalid"
    CONFLICT = "conflict"
    REJECTED = "rejected"
    RETRIES_EXHAUSTED = "retries-exhausted"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """A delivery set aside unchanged, with what went wrong (ADR 0006)."""

    delivery: Delivery
    reason: DeadLetterReason
    detail: str  # JSON, at most 4 KB in UTF-8
    attempts: int  # handover attempts made
    event_id: UUID | None  # when it could be read
    order_id: UUID | None  # when it could be read

    @property
    def traceparent(self) -> str | None:
        """The traceparent to set, replacing the record's, when the consumer started the trace;
        otherwise the copied headers already carry it (ADR 0006, 0008)."""
        trace = self.delivery.trace
        return trace.traceparent if trace.started_here else None


class DeadLetterUnwritable(Exception):
    """The dead letter was not confirmed as written, so its offset can't be committed."""


class DeadLetterSink(Protocol):
    def write(self, dead_letter: DeadLetter) -> None:
        """Returns once the dead letter is durably written; raises DeadLetterUnwritable if it
        can't be."""
