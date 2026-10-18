"""Processing one delivery (ADR 0002, 0004, 0006): store, hand over, record, skip a duplicate, or
dead-letter an invalid or conflicting event, and report the outcome."""

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from order_consumer.application.ports import (
    AlreadyStored,
    DeadLetter,
    DeadLetterReason,
    Delivery,
    EventIdTaken,
    HandoverResult,
    HandoverStatus,
    ReceivedEvent,
    Stored,
)
from order_consumer.application.processing import OrderProcessor, Outcome
from order_consumer.domain.downstream import DownstreamRequest
from order_consumer.domain.event import OrderPlacedReader
from order_consumer.domain.order import Order, to_order
from tests.support.events import EVENT_SCHEMA, order_placed_envelope, order_placed_event

ORDER_ID = UUID("3f1c2a9e-8b4d-4c1e-9f2a-6d7e8f9a0b1c")
EVENT_ID = UUID("7d2e1f3a-4b5c-4d6e-8f9a-0b1c2d3e4f5a")
RE_EMITTED_EVENT_ID = UUID("9b04c6d8-2e1f-4a3b-8c5d-6e7f8a9b0c1d")

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

    def store_new(
        self, order: Order, event: ReceivedEvent
    ) -> Stored | AlreadyStored | EventIdTaken:
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
        stored = self.orders[order_id]
        stored.handover_status = "handed_over"
        stored.fulfilment_order_id = fulfilment_order_id


@dataclass
class FakeFulfilment:
    store: FakeOrderStore
    requests: list[DownstreamRequest] = field(default_factory=list)
    stored_before_handover: list[bool] = field(default_factory=list)

    def hand_over(self, request: DownstreamRequest) -> HandoverResult:
        self.stored_before_handover.append(UUID(request.idempotency_key) in self.store.orders)
        self.requests.append(request)
        return HandoverResult(fulfilment_order_id="ful-1")


@dataclass
class FakeDeadLetterSink:
    fails: bool = False
    written: list[DeadLetter] = field(default_factory=list)

    def write(self, dead_letter: DeadLetter) -> None:
        if self.fails:
            raise RuntimeError("the dead-letter topic can't be written")
        self.written.append(dead_letter)


@dataclass
class ProcessorWithFakes:
    """The processor with fakes behind every port."""

    store: FakeOrderStore = field(default_factory=FakeOrderStore)
    dead_letters: FakeDeadLetterSink = field(default_factory=FakeDeadLetterSink)
    fulfilment: FakeFulfilment = field(init=False)
    processor: OrderProcessor = field(init=False)

    def __post_init__(self) -> None:
        self.fulfilment = FakeFulfilment(self.store)
        self.processor = OrderProcessor(self.store, self.fulfilment, self.dead_letters, reader)

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
    assert harness.store.processed[EVENT_ID] == ProcessedEvent(
        ORDER_ID, "accepted", ReceivedEvent(EVENT_ID, "orders", 1, 42)
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
        ReceivedEvent(EVENT_ID, "orders", 1, 41),
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


def test_an_invalid_event_has_no_outcome_while_its_dead_letter_cant_be_written() -> None:
    harness = ProcessorWithFakes(dead_letters=FakeDeadLetterSink(fails=True))

    with pytest.raises(RuntimeError):
        harness.process(order_placed_event(ORDER_ID, EVENT_ID, total=1))
