"""Mapping 1 (ADR 0001): the data of an order placed event becomes our Order."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from hypothesis import given

from order_consumer.domain.order import OrderLine, to_order
from tests.support.strategies import order_data

ORDER_ID = "3f1c2a9e-8b4d-4c1e-9f2a-6d7e8f9a0b1c"
CUSTOMER_ID = "a9e2b7c4-1d3f-4e5a-8b6c-7d8e9f0a1b2c"


def event_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "orderId": ORDER_ID,
        "customerId": CUSTOMER_ID,
        "placedAt": "2026-10-17T11:12:03+02:00",
        "currency": "EUR",
        "lines": [
            {"sku": "KNE-ESC-0042", "quantity": 2, "unitPrice": 1999},
            {"sku": "KNE-ELV-0007", "quantity": 1, "unitPrice": 500},
        ],
        "total": 4498,
    }
    data.update(overrides)
    return data


def test_placement_time_is_normalised_to_utc() -> None:
    order = to_order(event_data())

    assert order.placed_at == datetime(2026, 10, 17, 9, 12, 3, tzinfo=UTC)
    assert order.placed_at.tzinfo is UTC


def test_line_totals_and_item_count_are_derived() -> None:
    order = to_order(event_data())

    assert [line.line_total for line in order.lines] == [3998, 500]
    assert order.item_count == 3


def test_identities_and_values_are_carried_over() -> None:
    order = to_order(event_data())

    assert order.order_id == UUID(ORDER_ID)
    assert order.customer_id == UUID(CUSTOMER_ID)
    assert order.currency == "EUR"
    assert order.total == 4498
    assert order.lines[0] == OrderLine(sku="KNE-ESC-0042", quantity=2, unit_price=1999)


def test_unknown_fields_are_dropped() -> None:
    order = to_order(
        event_data(
            promoCode="AUTUMN",
            lines=[
                {"sku": "KNE-ESC-0042", "quantity": 1, "unitPrice": 1999, "giftWrap": True},
            ],
        )
    )

    assert not hasattr(order, "promo_code")
    assert order.lines == (OrderLine(sku="KNE-ESC-0042", quantity=1, unit_price=1999),)


def test_whole_numbers_written_with_a_fraction_become_integers() -> None:
    order = to_order(
        event_data(
            lines=[{"sku": "KNE-ESC-0042", "quantity": 2.0, "unitPrice": 1999.0}], total=3998.0
        )
    )

    assert order.lines == (OrderLine(sku="KNE-ESC-0042", quantity=2, unit_price=1999),)
    assert [type(value) for value in (order.lines[0].quantity, order.lines[0].unit_price)] == [
        int,
        int,
    ]
    assert type(order.total) is int


@given(data=order_data())
def test_the_total_of_any_valid_order_is_the_sum_of_its_line_totals(
    data: dict[str, Any],
) -> None:
    order = to_order(data)

    assert order.total == sum(line.line_total for line in order.lines)
    assert order.item_count == sum(line["quantity"] for line in data["lines"])


@given(data=order_data())
def test_any_placement_time_keeps_its_instant_in_utc(data: dict[str, Any]) -> None:
    order = to_order(data)

    assert order.placed_at.tzinfo is UTC
    assert order.placed_at == datetime.fromisoformat(data["placedAt"])
