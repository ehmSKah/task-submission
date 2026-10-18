"""Processing one delivery (ADR 0004, 0006): store, hand over, record, or dead-letter an invalid
event, and report the outcome."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from order_consumer.application.ports import (
    DeadLetter,
    DeadLetterReason,
    Delivery,
    HandoverResult,
)
from order_consumer.application.processing import OrderProcessor, Outcome
from order_consumer.domain.downstream import DownstreamRequest
from order_consumer.domain.event import OrderPlacedReader
from order_consumer.domain.order import Order
from tests.support.events import order_placed_envelope, order_placed_event

SCHEMA = Path(__file__).parents[3] / "schemas" / "order-placed.v1.schema.json"
ORDER_ID = UUID("3f1c2a9e-8b4d-4c1e-9f2a-6d7e8f9a0b1c")
EVENT_ID = UUID("7d2e1f3a-4b5c-4d6e-8f9a-0b1c2d3e4f5a")

reader = OrderPlacedReader.from_file(SCHEMA)


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

    def store_new(self, order: Order, event_id: UUID) -> None:
        self.orders[order.order_id] = StoredOrder(order=order, first_event_id=event_id)

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
class Consumer:
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
    consumer = Consumer()

    outcome = consumer.process()

    assert outcome is Outcome.HANDED_OVER
    assert consumer.dead_letters.written == []
    stored = consumer.store.orders[ORDER_ID]
    assert stored.first_event_id == EVENT_ID
    assert stored.handover_status == "handed_over"
    assert stored.fulfilment_order_id == "ful-1"


def test_the_order_is_stored_before_it_is_handed_over() -> None:
    consumer = Consumer()

    consumer.process()

    assert consumer.fulfilment.stored_before_handover == [True]


def test_the_handover_carries_the_order_id_as_idempotency_key() -> None:
    consumer = Consumer()

    consumer.process()

    [request] = consumer.fulfilment.requests
    assert request.idempotency_key == str(ORDER_ID)
    assert request.payload["externalReference"] == str(ORDER_ID)


def test_an_invalid_event_is_dead_lettered_and_neither_stored_nor_handed_over() -> None:
    consumer = Consumer()
    value = order_placed_event(ORDER_ID, EVENT_ID, total=4499)

    outcome = consumer.process(value)

    assert outcome is Outcome.DEAD_LETTERED
    assert consumer.store.orders == {}
    assert consumer.fulfilment.requests == []
    [dead_letter] = consumer.dead_letters.written
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
    consumer = Consumer()

    outcome = consumer.process(b"\xff")

    assert outcome is Outcome.DEAD_LETTERED
    [dead_letter] = consumer.dead_letters.written
    assert (dead_letter.event_id, dead_letter.order_id) == (None, None)
    assert json.loads(dead_letter.detail)["layer"] == "unreadable"


def test_the_detail_of_a_dead_letter_fits_in_4_kb_and_still_counts_every_error() -> None:
    consumer = Consumer()
    lines = [{"sku": "€" * 100, "quantity": 0, "unitPrice": 1} for _ in range(10)]
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID, lines=lines)

    consumer.process(json.dumps(envelope).encode())

    [dead_letter] = consumer.dead_letters.written
    assert len(dead_letter.detail.encode()) <= 4096
    detail: dict[str, Any] = json.loads(dead_letter.detail)
    assert detail["errorCount"] == 30
    assert 0 < len(detail["errors"]) < 20


def test_an_invalid_event_has_no_outcome_while_its_dead_letter_cant_be_written() -> None:
    consumer = Consumer(dead_letters=FakeDeadLetterSink(fails=True))

    with pytest.raises(RuntimeError):
        consumer.process(order_placed_event(ORDER_ID, EVENT_ID, total=1))
