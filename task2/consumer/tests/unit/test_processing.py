"""Processing one delivery (ADR 0002, 0004, 0006): store, hand over with bounded retries, record,
skip a duplicate, or dead-letter an invalid or conflicting event or a failed handover, and report
the outcome. Every message is followed by its trace id (ADR 0008)."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from structlog.contextvars import merge_contextvars
from structlog.testing import capture_logs

from order_consumer.application.ports import (
    AlreadyStored,
    DeadLetter,
    DeadLetterReason,
    DeadLetterUnwritable,
    Delivery,
    EventIdTaken,
    HandoverFailed,
    HandoverResult,
    HandoverStatus,
    MessageTrace,
    ReceivedEvent,
    ResponseClass,
    Stored,
    StoreUnavailable,
    TraceSource,
)
from order_consumer.application.processing import OrderProcessor, Outcome, Stopped, StopReason
from order_consumer.application.retries import Jitter, RetryPolicy
from order_consumer.domain.downstream import DownstreamRequest
from order_consumer.domain.event import OrderPlacedReader
from order_consumer.domain.order import Order, to_order
from tests.support.events import EVENT_SCHEMA, order_placed_envelope, order_placed_event

ORDER_ID = UUID("3f1c2a9e-8b4d-4c1e-9f2a-6d7e8f9a0b1c")
EVENT_ID = UUID("7d2e1f3a-4b5c-4d6e-8f9a-0b1c2d3e4f5a")
RE_EMITTED_EVENT_ID = UUID("9b04c6d8-2e1f-4a3b-8c5d-6e7f8a9b0c1d")

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
# The record's traceparent, and the one naming the message's own span in the same trace.
RECORD_TRACEPARENT = f"00-{TRACE_ID}-b7ad6b7169203331-01"
CONTINUED = MessageTrace(TRACE_ID, f"00-{TRACE_ID}-53995c3f42cd8ad8-01", TraceSource.HEADER)
STARTED_HERE_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
STARTED_HERE_TRACEPARENT = f"00-{STARTED_HERE_TRACE_ID}-00f067aa0ba902b7-01"

reader = OrderPlacedReader.from_file(EVENT_SCHEMA)


def delivery(value: bytes | None = None, message_trace: MessageTrace = CONTINUED) -> Delivery:
    return Delivery(
        key=str(ORDER_ID).encode(),
        value=order_placed_event(ORDER_ID, EVENT_ID) if value is None else value,
        topic="orders",
        partition=1,
        offset=42,
        headers=(("traceparent", RECORD_TRACEPARENT.encode()),),
        timestamp=datetime(2026, 10, 17, 9, 12, 4, tzinfo=UTC),
        trace=message_trace,
    )


@dataclass
class StoredOrder:
    order: Order
    first_event_id: UUID
    handover_status: str = "awaiting_handover"
    fulfilment_order_id: str | None = None


@dataclass(frozen=True)
class ProcessedEvent:
    order_id: UUID
    outcome: str
    received: ReceivedEvent


@dataclass
class FakeOrderStore:
    orders: dict[UUID, StoredOrder] = field(default_factory=dict)
    processed: dict[UUID, ProcessedEvent] = field(default_factory=dict)
    stored: list[UUID] = field(default_factory=list)
    # The steps that find the database unavailable, e.g. {"record_handover"}.
    unavailable: set[str] = field(default_factory=set)

    def _reach(self, step: str) -> None:
        if step in self.unavailable:
            raise StoreUnavailable("connection refused")

    def store_new(
        self, order: Order, event: ReceivedEvent
    ) -> Stored | AlreadyStored | EventIdTaken:
        self._reach("store_new")
        existing = self.orders.get(order.order_id)
        recorded = self.processed.get(event.event_id)
        if recorded is not None and recorded.order_id != order.order_id:
            return EventIdTaken()
        if existing is not None:
            return AlreadyStored(
                order=existing.order,
                first_event_id=existing.first_event_id,
                handover_status=HandoverStatus(existing.handover_status),
                event_recorded=recorded is not None,
            )
        self.orders[order.order_id] = StoredOrder(order=order, first_event_id=event.event_id)
        self.processed[event.event_id] = ProcessedEvent(order.order_id, "accepted", event)
        self.stored.append(order.order_id)
        return Stored()

    def record_duplicate(self, order_id: UUID, event: ReceivedEvent) -> None:
        self.processed.setdefault(event.event_id, ProcessedEvent(order_id, "duplicate", event))

    def record_handover(self, order_id: UUID, fulfilment_order_id: str) -> None:
        self._reach("record_handover")
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
    failures: list[Exception] = field(default_factory=list)
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
            raise DeadLetterUnwritable("not confirmed in time")
        stored = self.store.orders.get(ORDER_ID) if self.store is not None else None
        self.handover_status_when_written.append(stored.handover_status if stored else None)
        self.written.append(dead_letter)


@dataclass
class ProcessorWithFakes:
    """The processor with fakes behind every port."""

    store: FakeOrderStore = field(default_factory=FakeOrderStore)
    dead_letters: FakeDeadLetterSink = field(default_factory=FakeDeadLetterSink)
    failures: list[Exception] = field(default_factory=list)
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

    def process(
        self, value: bytes | None = None, message_trace: MessageTrace = CONTINUED
    ) -> Outcome:
        return self.processor.process(delivery(value, message_trace))


def test_a_valid_event_is_stored_handed_over_and_recorded() -> None:
    harness = ProcessorWithFakes()

    outcome = harness.process()

    assert outcome is Outcome.HANDED_OVER
    assert harness.dead_letters.written == []
    stored = harness.store.orders[ORDER_ID]
    assert stored.first_event_id == EVENT_ID
    assert stored.handover_status == "handed_over"
    assert stored.fulfilment_order_id == "ful-1"
    assert harness.store.processed[EVENT_ID] == ProcessedEvent(
        ORDER_ID, "accepted", ReceivedEvent(EVENT_ID, "orders", 1, 42, TRACE_ID)
    )


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


def test_a_re_emission_listing_the_lines_differently_repeats_the_first_request() -> None:
    """The fulfilment service compares a repeated request with the first by its body (ADR 0003)."""
    harness = ProcessorWithFakes()
    harness.process()
    harness.store.orders[ORDER_ID].handover_status = "awaiting_handover"
    harness.store.orders[ORDER_ID].fulfilment_order_id = None
    lines = order_placed_envelope(ORDER_ID)["data"]["lines"][::-1]

    outcome = harness.process(order_placed_event(ORDER_ID, RE_EMITTED_EVENT_ID, lines=lines))

    assert outcome is Outcome.HANDED_OVER
    first, repeated = harness.fulfilment.requests
    assert repeated == first


def test_a_redelivery_of_an_order_already_handed_over_is_skipped() -> None:
    harness = ProcessorWithFakes()
    harness.process()

    with capture_logs() as logs:
        outcome = harness.process()

    assert outcome is Outcome.DUPLICATE
    assert len(harness.fulfilment.requests) == 1, "no second handover"
    assert harness.store.orders[ORDER_ID].fulfilment_order_id == "ful-1"
    assert [entry["duplicate"] for entry in logs if entry["event"] == "order.duplicate"] == [
        "redelivery"
    ]


def test_a_re_emission_of_an_order_already_handed_over_is_skipped_and_recorded() -> None:
    harness = ProcessorWithFakes()
    harness.process()

    with capture_logs() as logs:
        outcome = harness.process(order_placed_event(ORDER_ID, RE_EMITTED_EVENT_ID))

    assert outcome is Outcome.DUPLICATE
    assert len(harness.fulfilment.requests) == 1, "no second handover"
    assert harness.store.stored == [ORDER_ID]
    assert harness.store.processed[RE_EMITTED_EVENT_ID].outcome == "duplicate"
    assert [entry["duplicate"] for entry in logs if entry["event"] == "order.duplicate"] == [
        "re-emission"
    ]


CONFLICTING_LINES = [{"sku": "KNE-ESC-0042", "quantity": 3, "unitPrice": 1999}]


def test_a_conflicting_event_is_dead_lettered_and_the_first_version_stays() -> None:
    harness = ProcessorWithFakes()
    harness.process()
    first_version = harness.store.orders[ORDER_ID].order
    value = order_placed_event(ORDER_ID, RE_EMITTED_EVENT_ID, lines=CONFLICTING_LINES, total=5997)

    outcome = harness.process(value)

    assert outcome is Outcome.DEAD_LETTERED
    assert harness.store.orders[ORDER_ID].order == first_version
    assert len(harness.fulfilment.requests) == 1, "no handover of the conflicting version"
    assert RE_EMITTED_EVENT_ID not in harness.store.processed
    [dead_letter] = harness.dead_letters.written
    assert dead_letter.delivery == delivery(value)
    assert dead_letter.reason is DeadLetterReason.CONFLICT
    assert dead_letter.attempts == 0
    assert (dead_letter.event_id, dead_letter.order_id) == (RE_EMITTED_EVENT_ID, ORDER_ID)
    assert json.loads(dead_letter.detail) == {
        "acceptedEventId": str(EVENT_ID),
        "fields": ["/data/lines", "/data/total"],
    }


def test_an_event_redelivered_with_other_content_is_dead_lettered_as_a_conflict() -> None:
    harness = ProcessorWithFakes()
    harness.process()

    outcome = harness.process(order_placed_event(ORDER_ID, EVENT_ID, currency="GBP"))

    assert outcome is Outcome.DEAD_LETTERED
    [dead_letter] = harness.dead_letters.written
    assert dead_letter.reason is DeadLetterReason.CONFLICT
    assert json.loads(dead_letter.detail)["fields"] == ["/data/currency"]
    assert harness.store.processed[EVENT_ID].outcome == "accepted"


def test_a_conflicting_event_is_not_handed_over_while_the_first_handover_is_unfinished() -> None:
    harness = ProcessorWithFakes()
    harness.store.store_new(
        to_order(order_placed_envelope(ORDER_ID, EVENT_ID)["data"]),
        ReceivedEvent(EVENT_ID, "orders", 1, 41, TRACE_ID),
    )

    outcome = harness.process(
        order_placed_event(ORDER_ID, RE_EMITTED_EVENT_ID, lines=CONFLICTING_LINES, total=5997)
    )

    assert outcome is Outcome.DEAD_LETTERED
    assert harness.fulfilment.requests == []
    assert harness.store.orders[ORDER_ID].handover_status == "awaiting_handover"
    [dead_letter] = harness.dead_letters.written
    assert dead_letter.reason is DeadLetterReason.CONFLICT


def test_an_event_id_already_recorded_for_another_order_is_a_conflict() -> None:
    harness = ProcessorWithFakes()
    harness.process()
    other_order_id = UUID("5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d")
    value = order_placed_event(other_order_id, EVENT_ID)

    outcome = harness.processor.process(replace(delivery(value), key=str(other_order_id).encode()))

    assert outcome is Outcome.DEAD_LETTERED
    assert other_order_id not in harness.store.orders
    assert len(harness.fulfilment.requests) == 1, "only the first order's handover"
    [dead_letter] = harness.dead_letters.written
    assert dead_letter.reason is DeadLetterReason.CONFLICT
    assert dead_letter.order_id == other_order_id
    assert json.loads(dead_letter.detail) == {
        "acceptedEventId": str(EVENT_ID),
        "fields": ["/data/orderId"],
    }


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


def test_an_unwritable_dead_letter_stops_processing() -> None:
    """Nothing can be committed without the dead letter, so the only safe move is to stop
    (ADR 0006)."""
    harness = ProcessorWithFakes(dead_letters=FakeDeadLetterSink(fails=True))

    outcome = harness.process(order_placed_event(ORDER_ID, EVENT_ID, total=1))

    assert outcome == Stopped(StopReason.DEAD_LETTER_TOPIC_UNWRITABLE, "not confirmed in time")


def test_a_failed_handover_with_an_unwritable_dead_letter_stops_after_marking_it_failed() -> None:
    harness = ProcessorWithFakes(
        failures=[unavailable(retry_after=60.0)], dead_letters=FakeDeadLetterSink(fails=True)
    )

    outcome = harness.process()

    assert isinstance(outcome, Stopped)
    assert harness.store.orders[ORDER_ID].handover_status == "handover_failed"


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
    """Wrong for every order, not this one: the outcome asks the poll loop to stop, with nothing
    committed and nothing dead-lettered (ADR 0003, 0006)."""
    systemic = HandoverFailed(ResponseClass.SYSTEMIC, f"{status} Wrong", status=status)
    harness = ProcessorWithFakes(failures=[unavailable(), systemic])

    outcome = harness.process()

    assert outcome == Stopped(StopReason.FULFILMENT, f"{status} Wrong")
    assert len(harness.fulfilment.requests) == 2, "no attempt after the systemic answer"
    assert harness.store.orders[ORDER_ID].handover_status == "awaiting_handover"
    assert harness.dead_letters.written == []


def test_a_redelivery_completes_a_failed_handover() -> None:
    harness = ProcessorWithFakes(failures=[unavailable(retry_after=60.0)])
    harness.process()

    outcome = harness.process()

    assert outcome is Outcome.HANDED_OVER
    assert harness.store.orders[ORDER_ID].handover_status == "handed_over"
    assert harness.store.stored == [ORDER_ID]


def test_a_re_emission_is_recorded_with_its_own_trace_id() -> None:
    harness = ProcessorWithFakes()
    harness.process()
    own_trace = MessageTrace(STARTED_HERE_TRACE_ID, STARTED_HERE_TRACEPARENT, TraceSource.NEW)

    harness.process(order_placed_event(ORDER_ID, RE_EMITTED_EVENT_ID), own_trace)

    assert harness.store.processed[EVENT_ID].received.trace_id == TRACE_ID
    assert harness.store.processed[RE_EMITTED_EVENT_ID].received.trace_id == STARTED_HERE_TRACE_ID


@pytest.mark.parametrize("source", list(TraceSource))
def test_every_line_of_a_message_says_where_its_trace_came_from(source: TraceSource) -> None:
    harness = ProcessorWithFakes(failures=[unavailable()])

    with capture_logs(processors=[merge_contextvars]) as logs:
        harness.process(message_trace=MessageTrace(TRACE_ID, STARTED_HERE_TRACEPARENT, source))

    assert {entry["trace_source"] for entry in logs} == {source.value}
    [received] = [entry for entry in logs if entry["event"] == "event.received"]
    assert received["event_id"] == str(EVENT_ID)


@pytest.mark.parametrize(
    ("source", "traceparent"),
    [
        (TraceSource.HEADER, None),
        (TraceSource.NEW, STARTED_HERE_TRACEPARENT),
        (TraceSource.REPLACED, STARTED_HERE_TRACEPARENT),
    ],
)
def test_a_dead_letter_carries_the_trace_the_consumer_started(
    source: TraceSource, traceparent: str | None
) -> None:
    """With a valid traceparent, the copied headers already carry the trace."""
    harness = ProcessorWithFakes()
    message_trace = MessageTrace(STARTED_HERE_TRACE_ID, STARTED_HERE_TRACEPARENT, source)

    harness.process(order_placed_event(ORDER_ID, EVENT_ID, total=1), message_trace)

    [dead_letter] = harness.dead_letters.written
    assert dead_letter.traceparent == traceparent


def test_each_handover_attempt_is_a_span_with_its_outcome(spans: InMemorySpanExporter) -> None:
    timeout = HandoverFailed(ResponseClass.TRANSIENT, "ReadTimeout")
    harness = ProcessorWithFakes(failures=[unavailable(), timeout])

    harness.process()

    attempts = [span for span in spans.get_finished_spans() if span.name == "handover attempt"]
    assert [dict(span.attributes or {}) for span in attempts] == [
        {
            "handover.attempt": 1,
            "handover.max_attempts": 5,
            "handover.response_class": "transient",
            "handover.status": 503,
            "handover.error": "503 Service Unavailable",
        },
        {
            "handover.attempt": 2,
            "handover.max_attempts": 5,
            "handover.response_class": "transient",
            "handover.error": "ReadTimeout",
        },
        {"handover.attempt": 3, "handover.max_attempts": 5, "handover.response_class": "success"},
    ]
    assert [span.status.status_code for span in attempts] == [
        trace.StatusCode.ERROR,
        trace.StatusCode.ERROR,
        trace.StatusCode.UNSET,
    ]
    assert attempts[0].status.description == "503 Service Unavailable"


def test_a_failed_attempt_is_logged_inside_its_span(spans: InMemorySpanExporter) -> None:
    harness = ProcessorWithFakes(failures=[unavailable()])
    span_ids: list[int] = []

    def current_span_id(
        _logger: object, _method: str, event_dict: dict[str, Any]
    ) -> dict[str, Any]:
        if event_dict["event"] == "handover.attempt_failed":
            span_ids.append(trace.get_current_span().get_span_context().span_id)
        return event_dict

    with capture_logs(processors=[current_span_id]):
        harness.process()

    first_attempt = next(
        span for span in spans.get_finished_spans() if span.name == "handover attempt"
    )
    assert span_ids == [first_attempt.context.span_id]


TERMINAL_EVENTS = {
    Outcome.HANDED_OVER: "handover.completed",
    Outcome.DUPLICATE: "order.duplicate",
    Outcome.DEAD_LETTERED: "dead_letter.written",
}


def _handed_over_once(harness: ProcessorWithFakes) -> None:
    harness.process()


def _unfinished(harness: ProcessorWithFakes) -> None:
    harness.process()
    harness.store.orders[ORDER_ID].handover_status = "awaiting_handover"
    harness.store.orders[ORDER_ID].fulfilment_order_id = None


def _nothing(_harness: ProcessorWithFakes) -> None:
    pass


@pytest.mark.parametrize(
    ("before", "value", "failures", "outcome"),
    [
        (_nothing, None, [], Outcome.HANDED_OVER),
        (_nothing, None, [unavailable(), unavailable()], Outcome.HANDED_OVER),
        (_unfinished, None, [], Outcome.HANDED_OVER),
        (_handed_over_once, None, [], Outcome.DUPLICATE),
        (
            _handed_over_once,
            order_placed_event(ORDER_ID, RE_EMITTED_EVENT_ID),
            [],
            Outcome.DUPLICATE,
        ),
        (_nothing, b"\xff", [], Outcome.DEAD_LETTERED),
        (_nothing, order_placed_event(ORDER_ID, EVENT_ID, total=1), [], Outcome.DEAD_LETTERED),
        (
            _handed_over_once,
            order_placed_event(ORDER_ID, EVENT_ID, currency="GBP"),
            [],
            Outcome.DEAD_LETTERED,
        ),
        (_nothing, None, [unavailable(retry_after=60.0)], Outcome.DEAD_LETTERED),
        (_nothing, None, [unavailable() for _ in range(5)], Outcome.DEAD_LETTERED),
        (
            _nothing,
            None,
            [HandoverFailed(ResponseClass.REJECTED, "422", status=422)],
            Outcome.DEAD_LETTERED,
        ),
    ],
    ids=[
        "handed over",
        "handed over after retries",
        "unfinished handover completed",
        "redelivery",
        "re-emission",
        "unreadable",
        "invalid",
        "conflicting",
        "long retry-after",
        "retries exhausted",
        "rejected",
    ],
)
def test_every_message_with_an_outcome_logs_exactly_one_terminal_line(
    before: Callable[[ProcessorWithFakes], None],
    value: bytes | None,
    failures: list[HandoverFailed],
    outcome: Outcome,
) -> None:
    harness = ProcessorWithFakes()
    before(harness)
    harness.failures.extend(failures)

    with capture_logs() as logs:
        assert harness.process(value) is outcome

    terminal = [entry["event"] for entry in logs if entry["event"] in TERMINAL_EVENTS.values()]
    assert terminal == [TERMINAL_EVENTS[outcome]]


def test_a_stopped_message_logs_no_terminal_line() -> None:
    """Its offset isn't committed, so it is processed again and ends with its one terminal line
    then."""
    systemic = HandoverFailed(ResponseClass.SYSTEMIC, "401 Unauthorized", status=401)
    harness = ProcessorWithFakes(failures=[systemic])
    failing_sink = ProcessorWithFakes(dead_letters=FakeDeadLetterSink(fails=True))

    with capture_logs() as logs:
        assert isinstance(harness.process(), Stopped)
        assert isinstance(failing_sink.process(b"\xff"), Stopped)

    assert [entry for entry in logs if entry["event"] in TERMINAL_EVENTS.values()] == []


def test_an_unavailable_database_stops_processing_before_the_handover() -> None:
    harness = ProcessorWithFakes(store=FakeOrderStore(unavailable={"store_new"}))

    outcome = harness.process()

    assert outcome == Stopped(StopReason.POSTGRES_UNAVAILABLE, "connection refused")
    assert harness.fulfilment.requests == []
    assert harness.dead_letters.written == []


def test_a_database_lost_after_the_handover_stops_processing_until_it_is_recorded() -> None:
    """The probe repeats the handover, and the fulfilment service replays it (ADR 0003, 0004)."""
    harness = ProcessorWithFakes(store=FakeOrderStore(unavailable={"record_handover"}))

    assert harness.process() == Stopped(StopReason.POSTGRES_UNAVAILABLE, "connection refused")
    assert harness.store.orders[ORDER_ID].handover_status == "awaiting_handover"

    harness.store.unavailable.clear()
    assert harness.process() is Outcome.HANDED_OVER
    assert len(harness.fulfilment.requests) == 2


def test_an_unexpected_exception_is_dead_lettered_with_its_type_and_message() -> None:
    """A bug for this message: crashing would restart onto it forever (ADR 0006). The stack trace
    goes to the error log only."""
    harness = ProcessorWithFakes(failures=[KeyError("lines")])

    with capture_logs() as logs:
        outcome = harness.process()

    assert outcome is Outcome.DEAD_LETTERED
    [dead_letter] = harness.dead_letters.written
    assert dead_letter.delivery == delivery()
    assert dead_letter.reason is DeadLetterReason.UNEXPECTED
    assert json.loads(dead_letter.detail) == {"type": "KeyError", "message": "'lines'"}
    assert dead_letter.attempts == 1
    assert (dead_letter.event_id, dead_letter.order_id) == (EVENT_ID, ORDER_ID)
    [error] = [entry for entry in logs if entry["event"] == "processing.unexpected_error"]
    assert error["log_level"] == "error"
    assert error["exc_info"] is True


def test_processing_goes_on_after_an_unexpected_exception() -> None:
    harness = ProcessorWithFakes(failures=[RuntimeError("a bug")])
    harness.process()

    assert harness.process() is Outcome.HANDED_OVER


def test_an_unexpected_exception_before_the_event_could_be_read_is_dead_lettered_without_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = ProcessorWithFakes()

    def broken_reader(*_args: object) -> None:
        raise ValueError("a bug in the reader")

    monkeypatch.setattr(reader, "read", broken_reader)
    outcome = harness.process()

    assert outcome is Outcome.DEAD_LETTERED
    [dead_letter] = harness.dead_letters.written
    assert (dead_letter.event_id, dead_letter.order_id, dead_letter.attempts) == (None, None, 0)


def test_the_detail_of_an_unexpected_dead_letter_fits_in_4_kb() -> None:
    harness = ProcessorWithFakes(failures=[RuntimeError("€" * 3000)])

    harness.process()

    [dead_letter] = harness.dead_letters.written
    assert len(dead_letter.detail.encode()) <= 4096
    detail = json.loads(dead_letter.detail)
    assert detail["type"] == "RuntimeError"
    assert len(detail["message"]) > 1000


def test_an_unexpected_exception_whose_dead_letter_cant_be_written_stops_processing() -> None:
    harness = ProcessorWithFakes(
        failures=[RuntimeError("a bug")], dead_letters=FakeDeadLetterSink(fails=True)
    )

    assert isinstance(harness.process(), Stopped)
