from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from orders_api.domain.order import (
    FINAL_STATUSES,
    LineItem,
    NewOrder,
    Order,
    OrderStatus,
    StatusChangeNotAllowed,
    format_timestamp,
)
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


# Status changes (ADR 0004): forward only, no skipping, `DELIVERED` and `CANCELLED` are final.

ALLOWED = {
    (OrderStatus.PENDING, OrderStatus.CONFIRMED),
    (OrderStatus.CONFIRMED, OrderStatus.SHIPPED),
    (OrderStatus.SHIPPED, OrderStatus.DELIVERED),
    (OrderStatus.PENDING, OrderStatus.CANCELLED),
    (OrderStatus.CONFIRMED, OrderStatus.CANCELLED),
}
DISALLOWED = sorted(
    (current, requested)
    for current in OrderStatus
    for requested in OrderStatus
    if current != requested and (current, requested) not in ALLOWED
)
LATER = datetime(2026, 10, 18, 8, 0, 0, 250000, tzinfo=UTC)


@pytest.mark.parametrize(("current", "requested"), sorted(ALLOWED))
def test_an_allowed_change_moves_to_the_next_version_updated_now(
    current: OrderStatus, requested: OrderStatus
) -> None:
    order = an_order(status=current, version=4)

    changed = order.with_status(requested, now=LATER)

    assert changed == replace(
        order, status=requested, updated_at="2026-10-18T08:00:00.250Z", version=5
    )


@pytest.mark.parametrize(("current", "requested"), DISALLOWED)
def test_every_other_change_is_not_allowed(current: OrderStatus, requested: OrderStatus) -> None:
    order = an_order(status=current)

    with pytest.raises(StatusChangeNotAllowed) as refused:
        order.with_status(requested, now=LATER)

    assert (refused.value.current, refused.value.requested) == (current, requested)
    assert str(refused.value) == f"An order can't change from {current} to {requested}"


def test_fifteen_changes_are_not_allowed() -> None:
    assert len(DISALLOWED) == 5 * 4 - len(ALLOWED) == 15


@pytest.mark.parametrize("status", list(OrderStatus))
def test_requesting_the_current_status_changes_nothing(status: OrderStatus) -> None:
    order = an_order(status=status, version=3)

    assert order.with_status(status, now=LATER) is order


def test_only_delivered_and_cancelled_are_final() -> None:
    assert {OrderStatus.DELIVERED, OrderStatus.CANCELLED} == FINAL_STATUSES
