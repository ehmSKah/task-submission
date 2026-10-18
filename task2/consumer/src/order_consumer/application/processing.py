"""Processing one delivery: read it, then store, hand over and record (ADR 0004), or set an invalid
event or a failed handover aside as a dead letter (ADR 0006).

Returns an Outcome and never commits: only the poll loop commits, according to the outcome
(ADR 0005).
"""

import json
from enum import Enum
from uuid import UUID

import structlog

from order_consumer.application.ports import (
    Clock,
    DeadLetter,
    DeadLetterReason,
    DeadLetterSink,
    Delivery,
    FulfilmentClient,
    HandoverStatus,
    OrderStore,
)
from order_consumer.application.retries import (
    GaveUp,
    HandoverRetries,
    Jitter,
    RetryPolicy,
)
from order_consumer.domain.downstream import to_downstream_request
from order_consumer.domain.event import OrderPlacedReader
from order_consumer.domain.order import to_order
from order_consumer.domain.validation import InvalidEvent

log = structlog.get_logger()

MAX_DETAIL_BYTES = 4096


class Outcome(Enum):
    HANDED_OVER = "handed_over"
    DUPLICATE = "duplicate"  # already handed over: no new effects
    DEAD_LETTERED = "dead_lettered"


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
    ) -> None:
        self._store = store
        self._handover = HandoverRetries(fulfilment, policy, clock, jitter)
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

                if self._store.store_new(order, event.event_id):
                    log.info("order.accepted")
                else:
                    # Already stored (ADR 0004): complete an unfinished handover, nothing more.
                    status = self._store.handover_status(order.order_id)
                    if status is HandoverStatus.HANDED_OVER:
                        log.info("order.duplicate")
                        return Outcome.DUPLICATE
                    log.info("handover.unfinished", handover_status=status.value)

                handover = self._handover.hand_over(to_downstream_request(order))
                if isinstance(handover, GaveUp):
                    return self._give_up(delivery, event.event_id, order.order_id, handover)
                fulfilment_order_id = handover.result.fulfilment_order_id
                self._store.record_handover(order.order_id, fulfilment_order_id)
                log.info(
                    "handover.completed",
                    fulfilment_order_id=fulfilment_order_id,
                    attempts=handover.attempts,
                )
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

    def _give_up(
        self, delivery: Delivery, event_id: UUID, order_id: UUID, gave_up: GaveUp
    ) -> Outcome:
        """A failed handover (ADR 0004): `handover_failed` is set before the dead letter is
        written, so a crash in between only causes a redelivery and another attempt."""
        self._store.mark_handover_failed(order_id)
        log.warning("handover.failed", reason=gave_up.reason.value, attempts=gave_up.attempts)
        self._dead_letters.write(
            DeadLetter(
                delivery=delivery,
                reason=gave_up.reason,
                detail=_handover_detail(gave_up),
                attempts=gave_up.attempts,
                event_id=event_id,
                order_id=order_id,
            )
        )
        log.warning("dead_letter.written", reason=gave_up.reason.value)
        return Outcome.DEAD_LETTERED


def _handover_detail(gave_up: GaveUp) -> str:
    """The last attempt's status and error, a Retry-After too long to wait for, and for a rejection
    the start of the problem body, cut until the detail fits in 4 KB."""
    failure = gave_up.last_failure
    detail: dict[str, object] = {"status": failure.status, "error": failure.error}
    if gave_up.retry_after is not None:
        detail["retryAfterSeconds"] = gave_up.retry_after
    problem = failure.problem
    while True:
        if problem is not None:
            detail["problem"] = problem
        encoded = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode()) <= MAX_DETAIL_BYTES or not problem:
            return encoded
        problem = problem[: len(problem) * 3 // 4]


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
