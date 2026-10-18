"""Processing one delivery (ADR 0004, 0006): store, hand over with bounded retries, record, or
dead-letter an invalid event or a failed handover, and report the outcome."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from order_consumer.application.ports import (
    DeadLetter,
    DeadLetterReason,
    Delivery,
    HandoverFailed,
    HandoverResult,
    HandoverStatus,
    ResponseClass,
)
from order_consumer.application.processing import OrderProcessor, Outcome
from order_consumer.application.retries import Jitter, RetryPolicy, SystemicFailure
from order_consumer.domain.downstream import DownstreamRequest
from order_consumer.domain.event import OrderPlacedReader
from order_consumer.domain.order import Order
from tests.support.events import EVENT_SCHEMA, order_placed_envelope, order_placed_event

ORDER_ID = UUID("3f1c2a9e-8b4d-4c1e-9f2a-6d7e8f9a0b1c")
EVENT_ID = UUID("7d2e1f3a-4b5c-4d6e-8f9a-0b1c2d3e4f5a")

reader = OrderPlacedReader.from_file(EVENT_SCHEMA)


def delivery(value: bytes | None = None) -> Delivery:
    return Delivery(
        key=str(ORDER_ID).encode(),
        value=order_placed_event(ORDER_ID, EVENT_ID) if value is None else value,
        topic="orders",
        partition=1,
        offset=42,
        headers=(("traceparent", b"00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"),),
        timestamp=datetime(2026, 10, 17, 9, 12, 4, tzinfo=UTC),
    )


@dataclass
class StoredOrder:
    order: Order
    first_event_id: UUID
    handover_status: str = "awaiting_handover"
    fulfilment_order_id: str | None = None


@dataclass
class FakeOrderStore:
    orders: dict[UUID, StoredOrder] = field(default_factory=dict)
    stored: list[UUID] = field(default_factory=list)

    def store_new(self, order: Order, event_id: UUID) -> bool:
        if order.order_id in self.orders:
            return False
        self.orders[order.order_id] = StoredOrder(order=order, first_event_id=event_id)
        self.stored.append(order.order_id)
        return True

    def handover_status(self, order_id: UUID) -> HandoverStatus:
        return HandoverStatus(self.orders[order_id].handover_status)

    def record_handover(self, order_id: UUID, fulfilment_order_id: str) -> None:
        stored = self.orders[order_id]
        stored.handover_status = "handed_over"
        stored.fulfilment_order_id = fulfilment_order_id

    def mark_handover_failed(self, order_id: UUID) -> None:
        self.orders[order_id].handover_status = "handover_failed"


def unavailable(retry_after: float | None = None) -> HandoverFailed:
    return HandoverFailed(
        ResponseClass.TRANSIENT, "503 Service Unavailable", status=503, retry_after=retry_after
    )


@dataclass
class FakeFulfilment:
    """Answers each attempt with the next scripted failure, and succeeds once they are used up."""

    store: FakeOrderStore
    failures: list[HandoverFailed] = field(default_factory=list)
    requests: list[DownstreamRequest] = field(default_factory=list)
    stored_before_handover: list[bool] = field(default_factory=list)

    def hand_over(self, request: DownstreamRequest) -> HandoverResult:
        self.stored_before_handover.append(UUID(request.idempotency_key) in self.store.orders)
        self.requests.append(request)
        if self.failures:
            raise self.failures.pop(0)
        return HandoverResult(fulfilment_order_id="ful-1")


@dataclass
class FakeClock:
    """Time passes only when the processor waits."""

    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def longest(cap: float) -> float:
    """Jitter pinned to the longest wait allowed."""
    return cap


@dataclass
class FakeDeadLetterSink:
    fails: bool = False
    written: list[DeadLetter] = field(default_factory=list)
    store: FakeOrderStore | None = None
    # The order's handover status at the moment each dead letter was written.
    handover_status_when_written: list[str | None] = field(default_factory=list)

    def write(self, dead_letter: DeadLetter) -> None:
        if self.fails:
            raise RuntimeError("the dead-letter topic can't be written")
        stored = self.store.orders.get(ORDER_ID) if self.store is not None else None
        self.handover_status_when_written.append(stored.handover_status if stored else None)
        self.written.append(dead_letter)


@dataclass
class ProcessorWithFakes:
    """The processor with fakes behind every port."""

    store: FakeOrderStore = field(default_factory=FakeOrderStore)
    dead_letters: FakeDeadLetterSink = field(default_factory=FakeDeadLetterSink)
    failures: list[HandoverFailed] = field(default_factory=list)
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    jitter: Jitter = longest
    clock: FakeClock = field(default_factory=FakeClock)
    fulfilment: FakeFulfilment = field(init=False)
    processor: OrderProcessor = field(init=False)

    def __post_init__(self) -> None:
        self.dead_letters.store = self.store
        self.fulfilment = FakeFulfilment(self.store, self.failures)
        self.processor = OrderProcessor(
            self.store,
            self.fulfilment,
            self.dead_letters,
            reader,
            policy=self.policy,
            clock=self.clock,
            jitter=self.jitter,
        )

    def process(self, value: bytes | None = None) -> Outcome:
        return self.processor.process(delivery(value))


def test_a_valid_event_is_stored_handed_over_and_recorded() -> None:
    harness = ProcessorWithFakes()

    outcome = harness.process()

    assert outcome is Outcome.HANDED_OVER
    assert harness.dead_letters.written == []
    stored = harness.store.orders[ORDER_ID]
    assert stored.first_event_id == EVENT_ID
    assert stored.handover_status == "handed_over"
    assert stored.fulfilment_order_id == "ful-1"


def test_the_order_is_stored_before_it_is_handed_over() -> None:
    harness = ProcessorWithFakes()

    harness.process()

    assert harness.fulfilment.stored_before_handover == [True]


def test_the_handover_carries_the_order_id_as_idempotency_key() -> None:
    harness = ProcessorWithFakes()

    harness.process()

    [request] = harness.fulfilment.requests
    assert request.idempotency_key == str(ORDER_ID)
    assert request.payload["externalReference"] == str(ORDER_ID)


def test_a_redelivery_completes_an_unfinished_handover() -> None:
    harness = ProcessorWithFakes()
    harness.process()
    harness.store.orders[ORDER_ID].handover_status = "awaiting_handover"
    harness.store.orders[ORDER_ID].fulfilment_order_id = None

    outcome = harness.process()

    assert outcome is Outcome.HANDED_OVER
    assert len(harness.fulfilment.requests) == 2
    assert harness.store.orders[ORDER_ID].handover_status == "handed_over"
    assert harness.store.stored == [ORDER_ID], "the order must be stored only once"


def test_a_redelivery_of_an_order_already_handed_over_is_skipped() -> None:
    harness = ProcessorWithFakes()
    harness.process()

    outcome = harness.process()

    assert outcome is Outcome.DUPLICATE
    assert len(harness.fulfilment.requests) == 1, "no second handover"
    assert harness.store.orders[ORDER_ID].fulfilment_order_id == "ful-1"


def test_an_invalid_event_is_dead_lettered_and_neither_stored_nor_handed_over() -> None:
    harness = ProcessorWithFakes()
    value = order_placed_event(ORDER_ID, EVENT_ID, total=4499)

    outcome = harness.process(value)

    assert outcome is Outcome.DEAD_LETTERED
    assert harness.store.orders == {}
    assert harness.fulfilment.requests == []
    [dead_letter] = harness.dead_letters.written
    assert dead_letter.delivery == delivery(value)
    assert dead_letter.reason is DeadLetterReason.INVALID
    assert dead_letter.attempts == 0
    assert (dead_letter.event_id, dead_letter.order_id) == (EVENT_ID, ORDER_ID)
    assert json.loads(dead_letter.detail) == {
        "layer": "business-rule",
        "errorCount": 1,
        "errors": [
            {"pointer": "/data/total", "message": "4499 is not the sum of the line totals, 4498"}
        ],
    }


def test_an_unreadable_event_is_dead_lettered_without_ids() -> None:
    harness = ProcessorWithFakes()

    outcome = harness.process(b"\xff")

    assert outcome is Outcome.DEAD_LETTERED
    [dead_letter] = harness.dead_letters.written
    assert (dead_letter.event_id, dead_letter.order_id) == (None, None)
    assert json.loads(dead_letter.detail)["layer"] == "unreadable"


def test_the_detail_of_a_dead_letter_fits_in_4_kb_and_still_counts_every_error() -> None:
    harness = ProcessorWithFakes()
    lines = [{"sku": "€" * 100, "quantity": 0, "unitPrice": 1} for _ in range(10)]
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID, lines=lines)

    harness.process(json.dumps(envelope).encode())

    [dead_letter] = harness.dead_letters.written
    assert len(dead_letter.detail.encode()) <= 4096
    detail: dict[str, Any] = json.loads(dead_letter.detail)
    assert detail["errorCount"] == 30
    assert 0 < len(detail["errors"]) < 20


def test_an_invalid_event_has_no_outcome_while_its_dead_letter_cant_be_written() -> None:
    harness = ProcessorWithFakes(dead_letters=FakeDeadLetterSink(fails=True))

    with pytest.raises(RuntimeError):
        harness.process(order_placed_event(ORDER_ID, EVENT_ID, total=1))


def test_two_transient_failures_then_success_end_handed_over_after_three_attempts() -> None:
    harness = ProcessorWithFakes(failures=[unavailable(), unavailable()])

    outcome = harness.process()

    assert outcome is Outcome.HANDED_OVER
    assert len(harness.fulfilment.requests) == 3
    assert harness.clock.sleeps == [1.0, 2.0], "full-jitter backoff, pinned to its caps"
    assert harness.store.orders[ORDER_ID].handover_status == "handed_over"
    assert harness.dead_letters.written == []


def test_a_handover_that_keeps_failing_ends_failed_and_dead_lettered() -> None:
    timeout = HandoverFailed(ResponseClass.TRANSIENT, "ReadTimeout")
    harness = ProcessorWithFakes(failures=[*(unavailable() for _ in range(4)), timeout])

    outcome = harness.process()

    assert outcome is Outcome.DEAD_LETTERED
    assert len(harness.fulfilment.requests) == 5
    assert harness.clock.sleeps == [1.0, 2.0, 4.0, 8.0]
    assert harness.store.orders[ORDER_ID].handover_status == "handover_failed"
    [dead_letter] = harness.dead_letters.written
    assert harness.dead_letters.handover_status_when_written == ["handover_failed"]
    assert dead_letter.delivery == delivery()
    assert dead_letter.reason is DeadLetterReason.RETRIES_EXHAUSTED
    assert dead_letter.attempts == 5
    assert (dead_letter.event_id, dead_letter.order_id) == (EVENT_ID, ORDER_ID)
    assert json.loads(dead_letter.detail) == {"status": None, "error": "ReadTimeout"}


@pytest.mark.parametrize(
    ("status", "error"), [(400, "400 Bad Request"), (422, "422 Unprocessable Content")]
)
def test_a_rejected_handover_is_dead_lettered_without_retries(status: int, error: str) -> None:
    problem = '{"title":"Unprocessable Content","status":422}'
    rejected = HandoverFailed(ResponseClass.REJECTED, error, status=status, problem=problem)
    harness = ProcessorWithFakes(failures=[rejected])

    outcome = harness.process()

    assert outcome is Outcome.DEAD_LETTERED
    assert len(harness.fulfilment.requests) == 1
    assert harness.clock.sleeps == []
    assert harness.store.orders[ORDER_ID].handover_status == "handover_failed"
    assert harness.dead_letters.handover_status_when_written == ["handover_failed"]
    [dead_letter] = harness.dead_letters.written
    assert dead_letter.reason is DeadLetterReason.REJECTED
    assert dead_letter.attempts == 1
    assert json.loads(dead_letter.detail) == {"status": status, "error": error, "problem": problem}


def test_a_long_problem_body_is_cut_so_the_detail_fits_in_4_kb() -> None:
    problem = '{"detail":"' + "€" * 3000 + '"}'
    rejected = HandoverFailed(
        ResponseClass.REJECTED, "400 Bad Request", status=400, problem=problem
    )
    harness = ProcessorWithFakes(failures=[rejected])

    harness.process()

    [dead_letter] = harness.dead_letters.written
    assert len(dead_letter.detail.encode()) <= 4096
    detail = json.loads(dead_letter.detail)
    assert len(detail["problem"]) > 1000
    assert problem.startswith(detail["problem"])


def test_a_retry_after_up_to_15_seconds_is_waited_for_instead_of_the_backoff() -> None:
    harness = ProcessorWithFakes(failures=[unavailable(retry_after=12.0), unavailable(0.0)])

    outcome = harness.process()

    assert outcome is Outcome.HANDED_OVER
    assert harness.clock.sleeps == [12.0, 0.0]


def test_a_retry_after_of_exactly_15_seconds_is_still_waited_for() -> None:
    harness = ProcessorWithFakes(failures=[unavailable(retry_after=15.0)])

    assert harness.process() is Outcome.HANDED_OVER
    assert harness.clock.sleeps == [15.0]


def test_a_retry_after_just_over_15_seconds_ends_the_retries() -> None:
    harness = ProcessorWithFakes(failures=[unavailable(retry_after=16.0)])

    assert harness.process() is Outcome.DEAD_LETTERED
    assert harness.clock.sleeps == []


def test_a_retry_after_longer_than_15_seconds_ends_the_retries_at_once() -> None:
    harness = ProcessorWithFakes(failures=[unavailable(retry_after=120.0)])

    outcome = harness.process()

    assert outcome is Outcome.DEAD_LETTERED
    assert len(harness.fulfilment.requests) == 1
    assert harness.clock.sleeps == []
    assert harness.store.orders[ORDER_ID].handover_status == "handover_failed"
    [dead_letter] = harness.dead_letters.written
    assert dead_letter.reason is DeadLetterReason.RETRIES_EXHAUSTED
    assert dead_letter.attempts == 1
    assert json.loads(dead_letter.detail) == {
        "status": 503,
        "error": "503 Service Unavailable",
        "retryAfterSeconds": 120.0,
    }


@pytest.mark.parametrize("status", [401, 404, 302, 200])
def test_a_systemic_answer_stops_processing_with_nothing_written(status: int) -> None:
    """Stopping consumption on it is the poll loop's business; until it pauses, the failure
    propagates, so nothing is committed and the event is redelivered (ADR 0003, 0006)."""
    systemic = HandoverFailed(ResponseClass.SYSTEMIC, f"{status}", status=status)
    harness = ProcessorWithFakes(failures=[unavailable(), systemic])

    with pytest.raises(SystemicFailure) as stopped:
        harness.process()

    assert stopped.value.attempts == 2, "no attempt after the systemic answer"
    assert stopped.value.failure is systemic
    assert harness.store.orders[ORDER_ID].handover_status == "awaiting_handover"
    assert harness.dead_letters.written == []


def test_a_redelivery_completes_a_failed_handover() -> None:
    harness = ProcessorWithFakes(failures=[unavailable(retry_after=60.0)])
    harness.process()

    outcome = harness.process()

    assert outcome is Outcome.HANDED_OVER
    assert harness.store.orders[ORDER_ID].handover_status == "handed_over"
    assert harness.store.stored == [ORDER_ID]
