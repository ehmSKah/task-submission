from datetime import UTC, datetime

from orders_api.application.orders import create_order, get_order
from orders_api.domain.order import LineItem, NewOrder, OrderStatus
from tests.unit.fakes import FixedClock, InMemoryOrderRepository, SequentialIds

NOW = datetime(2026, 10, 17, 9, 30, 0, 123456, tzinfo=UTC)
REQUEST = NewOrder(
    currency="EUR",
    items=(
        LineItem(sku="ABC-123", quantity=2, unit_price=1999),
        LineItem(sku="XYZ-9", quantity=1, unit_price=500),
    ),
)


def test_creates_a_pending_order_for_the_caller_and_stores_it() -> None:
    repository = InMemoryOrderRepository()
    ids = SequentialIds(["0e6bb2c6-0000-4000-8000-000000000001"])

    order = create_order(repository, FixedClock(NOW), ids, owner_id="alice-sub", request=REQUEST)

    assert order.id == "0e6bb2c6-0000-4000-8000-000000000001"
    assert order.owner_id == "alice-sub"
    assert order.status is OrderStatus.PENDING
    assert order.version == 1
    assert order.items == REQUEST.items
    assert order.total_amount == 4498
    assert order.created_at == order.updated_at == "2026-10-17T09:30:00.123Z"
    assert get_order(repository, order.id) == order


def test_every_order_gets_a_new_id() -> None:
    repository = InMemoryOrderRepository()
    ids = SequentialIds(
        ["0e6bb2c6-0000-4000-8000-000000000001", "0e6bb2c6-0000-4000-8000-000000000002"]
    )

    first = create_order(repository, FixedClock(NOW), ids, owner_id="alice-sub", request=REQUEST)
    second = create_order(repository, FixedClock(NOW), ids, owner_id="alice-sub", request=REQUEST)

    assert first.id != second.id
    assert get_order(repository, first.id) == first
    assert get_order(repository, second.id) == second
