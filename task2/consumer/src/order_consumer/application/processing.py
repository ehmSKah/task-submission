"""Processing one delivery: read it, then store, hand over and record (ADR 0004), skip a duplicate
(ADR 0002), set an invalid or conflicting event, a failed handover or an unexpected exception aside
as a dead letter, or ask for consumption to stop when the failure is not about this message,
including when the flood guard sees failures repeat from message to message (ADR 0006), or end a
handover's retries early when shutdown is requested (ADR 0007).

Returns an Outcome and never commits: only the poll loop commits, according to the outcome
(ADR 0005).
"""

import json
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

import structlog

from order_consumer.application.flood_guard import FloodGuard, FloodGuardThresholds
from order_consumer.application.ports import (
    AlreadyStored,
    Clock,
    DeadLetter,
    DeadLetterReason,
    DeadLetterSink,
    DeadLetterUnwritable,
    Delivery,
    EventIdTaken,
    FulfilmentClient,
    HandoverResult,
    HandoverStatus,
    OrderStore,
    ReceivedEvent,
    StopSignal,
    Stored,
    StoreUnavailable,
)
from order_consumer.application.retries import (
    Abandoned,
    GaveUp,
    HandoverRetries,
    Jitter,
    RetryPolicy,
    SystemicAnswer,
)
from order_consumer.domain.downstream import DownstreamRequest, to_downstream_request
from order_consumer.domain.event import OrderPlacedReader
from order_consumer.domain.order import differing_fields, to_order
from order_consumer.domain.validation import InvalidEvent

log = structlog.get_logger()

MAX_DETAIL_BYTES = 4096


class Outcome(Enum):
    """Terminal: the poll loop commits the offset."""

    HANDED_OVER = "handed_over"
    DUPLICATE = "duplicate"  # already handed over: no new effects
    DEAD_LETTERED = "dead_lettered"


class StopReason(Enum):
    """Why consumption stops: what a paused instance shows (ADR 0006, 0007)."""

    FULFILMENT = "systemic: fulfilment"  # a systemic answer, e.g. to a wrong token
    POSTGRES_UNAVAILABLE = "systemic: postgres-unavailable"
    DEAD_LETTER_TOPIC_UNWRITABLE = "systemic: dead-letter-topic-unwritable"
    CIRCUIT_OPEN = "circuit-open"  # orders keep running out of retries: an outage
    BUG_GUARD = "bug-guard"  # messages keep meeting unexpected exceptions: a bug


@dataclass(frozen=True, slots=True)
class Stopped:
    """Not terminal: the failure is not about this message, so consumption stops with nothing
    committed and nothing dead-lettered, and the message is tried again later (ADR 0006)."""

    reason: StopReason
    error: str  # what happened, e.g. "401 Unauthorized"


@dataclass(frozen=True, slots=True)
class Interrupted:
    """Not terminal: shutdown was requested while the handover was being retried, so it ended
    after the attempt in progress. Nothing is committed or dead-lettered; the order stays awaiting
    its handover, and the redelivered message completes it (ADR 0004, 0007)."""


@dataclass
class _Progress:
    """What is known about the delivery in hand, for its dead letter should an unexpected exception
    end its processing."""

    event_id: UUID | None = None
    order_id: UUID | None = None
    attempts: int = 0  # handover attempts started


class _CountingAttempts:
    def __init__(self, fulfilment: FulfilmentClient) -> None:
        self._fulfilment = fulfilment
        self.progress = _Progress()

    def hand_over(self, request: DownstreamRequest) -> HandoverResult:
        self.progress.attempts += 1
        return self._fulfilment.hand_over(request)


class OrderProcessor:
    def __init__(
        self,
        store: OrderStore,
        fulfilment: FulfilmentClient,
        dead_letters: DeadLetterSink,
        reader: OrderPlacedReader,
        *,
        policy: RetryPolicy,
        clock: Clock,
        jitter: Jitter,
        stop: StopSignal,
        thresholds: FloodGuardThresholds | None = None,
    ) -> None:
        self._store = store
        self._fulfilment = _CountingAttempts(fulfilment)
        self._handover = HandoverRetries(self._fulfilment, policy, clock, jitter, stop)
        self._dead_letters = dead_letters
        self._reader = reader
        self._guard = FloodGuard(thresholds or FloodGuardThresholds())

    def process(self, delivery: Delivery) -> Outcome | Stopped | Interrupted:
        with structlog.contextvars.bound_contextvars(
            topic=delivery.topic,
            partition=delivery.partition,
            offset=delivery.offset,
            trace_source=delivery.trace.source.value,
        ):
            progress = self._fulfilment.progress = _Progress()
            try:
                return self._process(delivery, progress)
            except StoreUnavailable as unavailable:
                return Stopped(StopReason.POSTGRES_UNAVAILABLE, str(unavailable))
            except Exception as error:
                return self._set_aside_unexpected(delivery, progress, error)

    def _process(self, delivery: Delivery, progress: _Progress) -> Outcome | Stopped | Interrupted:
        try:
            event = self._reader.read(delivery.key, delivery.value)
        except InvalidEvent as invalid:
            return self._set_aside(delivery, invalid)

        progress.event_id = event.event_id
        order = to_order(event.data)
        progress.order_id = order.order_id
        with structlog.contextvars.bound_contextvars(
            event_id=str(event.event_id), order_id=str(order.order_id)
        ):
            log.info("event.received")

            received = ReceivedEvent(
                event.event_id,
                delivery.topic,
                delivery.partition,
                delivery.offset,
                delivery.trace.trace_id,
            )
            match self._store.store_new(order, received):
                case Stored():
                    log.info("order.accepted")
                case AlreadyStored() as accepted:
                    # Compared before anything else: a conflicting event is never handed over,
                    # not even to complete the first version's unfinished handover.
                    if fields := differing_fields(accepted.order, order):
                        return self._set_aside_conflict(
                            delivery,
                            event.event_id,
                            order.order_id,
                            accepted.first_event_id,
                            fields,
                        )
                    duplicate = "redelivery" if accepted.event_recorded else "re-emission"
                    if not accepted.event_recorded:
                        self._store.record_duplicate(order.order_id, received)
                    # Skip, or complete an unfinished handover, nothing more (ADR 0004).
                    if accepted.handover_status is HandoverStatus.HANDED_OVER:
                        self._guard.processed()
                        log.info("order.duplicate", duplicate=duplicate)
                        return Outcome.DUPLICATE
                    log.info(
                        "handover.unfinished",
                        handover_status=accepted.handover_status.value,
                        duplicate=duplicate,
                    )
                case EventIdTaken():
                    # The same event id with other content (ADR 0002): here another order.
                    return self._set_aside_conflict(
                        delivery,
                        event.event_id,
                        order.order_id,
                        event.event_id,
                        ["/data/orderId"],
                    )

            handover = self._handover.hand_over(
                to_downstream_request(order), self._guard.attempt_limit()
            )
            if isinstance(handover, SystemicAnswer):
                return Stopped(StopReason.FULFILMENT, handover.failure.error)
            if isinstance(handover, GaveUp):
                return self._give_up(delivery, event.event_id, order.order_id, handover)
            if isinstance(handover, Abandoned):
                return Interrupted()
            self._guard.answered()
            fulfilment_order_id = handover.result.fulfilment_order_id
            self._store.record_handover(order.order_id, fulfilment_order_id)
            self._guard.processed()
            log.info(
                "handover.completed",
                fulfilment_order_id=fulfilment_order_id,
                attempts=handover.attempts,
            )
            return Outcome.HANDED_OVER

    def _set_aside(self, delivery: Delivery, invalid: InvalidEvent) -> Outcome | Stopped:
        """Invalid is permanent (ADR 0001): dead-lettered at once, never retried, nothing
        stored."""
        readable = {
            name: str(value)
            for name, value in (("event_id", invalid.event_id), ("order_id", invalid.order_id))
            if value is not None
        }
        with structlog.contextvars.bound_contextvars(**readable):
            log.info("event.received")
            log.warning("event.invalid", layer=invalid.layer.value, error_count=invalid.error_count)
            return self._write(
                DeadLetter(
                    delivery=delivery,
                    reason=DeadLetterReason.INVALID,
                    detail=_invalid_detail(invalid),
                    attempts=0,
                    event_id=invalid.event_id,
                    order_id=invalid.order_id,
                )
            )

    def _set_aside_conflict(
        self,
        delivery: Delivery,
        event_id: UUID,
        order_id: UUID,
        accepted_event_id: UUID,
        fields: list[str],
    ) -> Outcome | Stopped:
        """Conflicting is permanent (ADR 0002): the first accepted version stays, the event is not
        recorded, and it is dead-lettered at once with the fields that differ."""
        log.warning("order.conflict", fields=fields, accepted_event_id=str(accepted_event_id))
        return self._write(
            DeadLetter(
                delivery=delivery,
                reason=DeadLetterReason.CONFLICT,
                detail=json.dumps(
                    {"acceptedEventId": str(accepted_event_id), "fields": fields},
                    separators=(",", ":"),
                ),
                attempts=0,
                event_id=event_id,
                order_id=order_id,
            )
        )

    def _set_aside_unexpected(
        self, delivery: Delivery, progress: _Progress, error: Exception
    ) -> Outcome | Stopped:
        """A bug for this message (ADR 0006): dead-lettered at once with the exception's type and
        message. Crashing instead would restart onto the same message forever. The stack trace
        goes to the error log only, never into the dead letter. A message that trips the bug guard
        is held instead (ADR 0006)."""
        ids = {
            name: str(value)
            for name, value in (("event_id", progress.event_id), ("order_id", progress.order_id))
            if value is not None
        }
        with structlog.contextvars.bound_contextvars(**ids):
            log.error("processing.unexpected_error", error=type(error).__name__, exc_info=True)
            described = f"{type(error).__name__}: {error}"
            if self._guard.holds_unexpected(described):
                return Stopped(StopReason.BUG_GUARD, described)
            outcome = self._write(
                DeadLetter(
                    delivery=delivery,
                    reason=DeadLetterReason.UNEXPECTED,
                    detail=_unexpected_detail(error),
                    attempts=progress.attempts,
                    event_id=progress.event_id,
                    order_id=progress.order_id,
                )
            )
            if outcome is Outcome.DEAD_LETTERED:
                self._guard.unexpected_dead_lettered()
            return outcome

    def _write(self, dead_letter: DeadLetter) -> Outcome | Stopped:
        """Without the dead letter nothing can be committed, so an unwritable one stops
        consumption (ADR 0006)."""
        try:
            self._dead_letters.write(dead_letter)
        except DeadLetterUnwritable as unwritable:
            return Stopped(StopReason.DEAD_LETTER_TOPIC_UNWRITABLE, str(unwritable))
        log.warning("dead_letter.written", reason=dead_letter.reason.value)
        return Outcome.DEAD_LETTERED

    def _give_up(
        self, delivery: Delivery, event_id: UUID, order_id: UUID, gave_up: GaveUp
    ) -> Outcome | Stopped:
        """A failed handover (ADR 0004): `handover_failed` is set before the dead letter is
        written, so a crash in between only causes a redelivery and another attempt. An order that
        opens the circuit is held instead: neither marked failed nor dead-lettered (ADR 0006)."""
        exhausted = gave_up.reason is DeadLetterReason.RETRIES_EXHAUSTED
        if not exhausted:
            self._guard.answered()
        elif self._guard.holds_exhausted(gave_up.last_failure.error):
            return Stopped(StopReason.CIRCUIT_OPEN, gave_up.last_failure.error)
        self._store.mark_handover_failed(order_id)
        log.warning("handover.failed", reason=gave_up.reason.value, attempts=gave_up.attempts)
        outcome = self._write(
            DeadLetter(
                delivery=delivery,
                reason=gave_up.reason,
                detail=_handover_detail(gave_up),
                attempts=gave_up.attempts,
                event_id=event_id,
                order_id=order_id,
            )
        )
        if outcome is Outcome.DEAD_LETTERED:
            if exhausted:
                self._guard.exhausted_dead_lettered()
            self._guard.processed()
        return outcome


def _handover_detail(gave_up: GaveUp) -> str:
    """The last attempt's status and error, a Retry-After too long to wait for, and for a rejection
    the start of the problem body, cut until the detail fits in 4 KB."""
    failure = gave_up.last_failure
    detail: dict[str, object] = {"status": failure.status, "error": failure.error}
    if gave_up.retry_after is not None:
        detail["retryAfterSeconds"] = gave_up.retry_after
    return _fitted(detail, "problem", failure.problem)


def _unexpected_detail(error: Exception) -> str:
    """The exception's type and message, the message cut until the detail fits in 4 KB."""
    return _fitted({"type": type(error).__name__}, "message", str(error))


def _fitted(detail: dict[str, object], name: str, text: str | None) -> str:
    """The detail as JSON with `text` added as `name`, the text cut until it fits in 4 KB."""
    while True:
        if text is not None:
            detail[name] = text
        encoded = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode()) <= MAX_DETAIL_BYTES or not text:
            return encoded
        text = text[: len(text) * 3 // 4]


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
