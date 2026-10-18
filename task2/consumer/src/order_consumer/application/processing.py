"""Processing one delivery: read it, then store, hand over and record (ADR 0004), skip a duplicate
(ADR 0002), or set an invalid or conflicting event or a failed handover aside as a dead letter
(ADR 0006).

Returns an Outcome and never commits: only the poll loop commits, according to the outcome
(ADR 0005).
"""

import json
from enum import Enum
from uuid import UUID

import structlog

from order_consumer.application.ports import (
    AlreadyStored,
    Clock,
    DeadLetter,
    DeadLetterReason,
    DeadLetterSink,
    Delivery,
    EventIdTaken,
    FulfilmentClient,
    HandoverStatus,
    OrderStore,
    ReceivedEvent,
    Stored,
)
from order_consumer.application.retries import (
    GaveUp,
    HandoverRetries,
    Jitter,
    RetryPolicy,
)
from order_consumer.domain.downstream import to_downstream_request
from order_consumer.domain.event import OrderPlacedReader
from order_consumer.domain.order import differing_fields, to_order
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

                received = ReceivedEvent(
                    event.event_id, delivery.topic, delivery.partition, delivery.offset
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
    ) -> Outcome:
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

    def _write(self, dead_letter: DeadLetter) -> Outcome:
        """A failure to write the dead letter propagates, so nothing is committed (ADR 0006)."""
        self._dead_letters.write(dead_letter)
        log.warning("dead_letter.written", reason=dead_letter.reason.value)
        return Outcome.DEAD_LETTERED

    def _give_up(
        self, delivery: Delivery, event_id: UUID, order_id: UUID, gave_up: GaveUp
    ) -> Outcome:
        """A failed handover (ADR 0004): `handover_failed` is set before the dead letter is
        written, so a crash in between only causes a redelivery and another attempt."""
        self._store.mark_handover_failed(order_id)
        log.warning("handover.failed", reason=gave_up.reason.value, attempts=gave_up.attempts)
        return self._write(
            DeadLetter(
                delivery=delivery,
                reason=gave_up.reason,
                detail=_handover_detail(gave_up),
                attempts=gave_up.attempts,
                event_id=event_id,
                order_id=order_id,
            )
        )


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
