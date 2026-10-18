"""Processing one delivery (ADR 0004): store, hand over, record, and report the outcome."""

from dataclasses import dataclass, field
from uuid import UUID

from order_consumer.application.ports import HandoverResult
from order_consumer.application.processing import Delivery, OrderProcessor, Outcome
from order_consumer.domain.downstream import DownstreamRequest
from order_consumer.domain.order import Order
from tests.support.events import order_placed_event

ORDER_ID = UUID("3f1c2a9e-8b4d-4c1e-9f2a-6d7e8f9a0b1c")
EVENT_ID = UUID("7d2e1f3a-4b5c-4d6e-8f9a-0b1c2d3e4f5a")


def delivery() -> Delivery:
    return Delivery(
        key=str(ORDER_ID).encode(),
        value=order_placed_event(ORDER_ID, EVENT_ID),
        topic="orders",
        partition=1,
        offset=42,
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


def test_a_valid_event_is_stored_handed_over_and_recorded() -> None:
    store = FakeOrderStore()
    fulfilment = FakeFulfilment(store)

    outcome = OrderProcessor(store, fulfilment).process(delivery())

    assert outcome is Outcome.HANDED_OVER
    stored = store.orders[ORDER_ID]
    assert stored.first_event_id == EVENT_ID
    assert stored.handover_status == "handed_over"
    assert stored.fulfilment_order_id == "ful-1"


def test_the_order_is_stored_before_it_is_handed_over() -> None:
    store = FakeOrderStore()
    fulfilment = FakeFulfilment(store)

    OrderProcessor(store, fulfilment).process(delivery())

    assert fulfilment.stored_before_handover == [True]


def test_the_handover_carries_the_order_id_as_idempotency_key() -> None:
    store = FakeOrderStore()
    fulfilment = FakeFulfilment(store)

    OrderProcessor(store, fulfilment).process(delivery())

    [request] = fulfilment.requests
    assert request.idempotency_key == str(ORDER_ID)
    assert request.payload["externalReference"] == str(ORDER_ID)
