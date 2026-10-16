from datetime import UTC, datetime, timedelta, timezone

from orders_api.domain.order import LineItem, NewOrder, Order, OrderStatus, format_timestamp
from tests.builders import an_order

ITEMS = (
    LineItem(sku="ABC-123", quantity=2, unit_price=1999),
    LineItem(sku="XYZ-9", quantity=1, unit_price=500),
)


def test_total_is_the_sum_of_quantity_times_unit_price() -> None:
    order = an_order(items=ITEMS)

    assert order.total_amount == 4498


def test_total_of_free_items_is_zero() -> None:
    order = an_order(items=(LineItem(sku="FREE-1", quantity=3, unit_price=0),))

    assert order.total_amount == 0


def test_total_at_the_largest_allowed_order_stays_an_exact_integer() -> None:
    items = tuple(LineItem(sku=f"SKU-{n}", quantity=999, unit_price=1_000_000) for n in range(50))

    assert an_order(items=items).total_amount == 50 * 999 * 1_000_000


def test_timestamps_are_utc_with_fixed_width_milliseconds() -> None:
    assert format_timestamp(datetime(2026, 10, 17, 9, 30, 0, tzinfo=UTC)) == (
        "2026-10-17T09:30:00.000Z"
    )
    assert format_timestamp(datetime(2026, 1, 2, 3, 4, 5, 123987, tzinfo=UTC)) == (
        "2026-01-02T03:04:05.123Z"
    )


def test_timestamps_in_another_zone_are_converted_to_utc() -> None:
    cest = timezone(timedelta(hours=2))

    assert format_timestamp(datetime(2026, 10, 17, 11, 30, 0, 5000, tzinfo=cest)) == (
        "2026-10-17T09:30:00.005Z"
    )


def test_a_new_order_is_pending_at_version_1_owned_by_its_creator() -> None:
    now = datetime(2026, 10, 17, 9, 30, 0, 123000, tzinfo=UTC)

    order = Order.new(
        order_id="0e6bb2c6-0000-4000-8000-000000000000",
        owner_id="alice-sub",
        request=NewOrder(currency="EUR", items=ITEMS),
        now=now,
    )

    assert order == Order(
        id="0e6bb2c6-0000-4000-8000-000000000000",
        owner_id="alice-sub",
        status=OrderStatus.PENDING,
        currency="EUR",
        items=ITEMS,
        created_at="2026-10-17T09:30:00.123Z",
        updated_at="2026-10-17T09:30:00.123Z",
        version=1,
    )
    assert order.total_amount == 4498
