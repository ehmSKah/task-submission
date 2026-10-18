"""Comparing an incoming Order with the first accepted version (ADR 0002): only differences in the
normalised Order count, named as JSON Pointers into the event."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

from order_consumer.domain.order import Order, OrderLine, differing_fields

ACCEPTED = Order(
    order_id=UUID("3f1c2a9e-8b4d-4c1e-9f2a-6d7e8f9a0b1c"),
    customer_id=UUID("a9e2b7c4-1d3f-4e5a-8b6c-7d8e9f0a1b2c"),
    placed_at=datetime(2026, 10, 17, 9, 12, 3, tzinfo=UTC),
    currency="EUR",
    lines=(
        OrderLine(sku="KNE-ELV-0007", quantity=1, unit_price=500),
        OrderLine(sku="KNE-ESC-0042", quantity=2, unit_price=1999),
    ),
    total=4498,
)


def test_the_same_order_has_no_differences() -> None:
    assert differing_fields(ACCEPTED, ACCEPTED) == []


def test_lines_in_another_order_are_not_a_difference() -> None:
    assert differing_fields(ACCEPTED, replace(ACCEPTED, lines=ACCEPTED.lines[::-1])) == []


def test_the_same_instant_in_another_offset_is_not_a_difference() -> None:
    elsewhere = ACCEPTED.placed_at.astimezone(timezone(timedelta(hours=2)))

    assert differing_fields(ACCEPTED, replace(ACCEPTED, placed_at=elsewhere)) == []


def test_another_line_quantity_is_a_difference_in_the_lines() -> None:
    lines = (ACCEPTED.lines[0], OrderLine(sku="KNE-ESC-0042", quantity=3, unit_price=1999))

    assert differing_fields(ACCEPTED, replace(ACCEPTED, lines=lines, total=6497)) == [
        "/data/lines",
        "/data/total",
    ]


def test_every_differing_field_is_named_in_the_events_terms() -> None:
    incoming = replace(
        ACCEPTED,
        customer_id=UUID("00000000-0000-4000-8000-000000000000"),
        placed_at=ACCEPTED.placed_at + timedelta(seconds=1),
        currency="GBP",
        lines=ACCEPTED.lines[:1],
        total=500,
    )

    assert differing_fields(ACCEPTED, incoming) == [
        "/data/customerId",
        "/data/placedAt",
        "/data/currency",
        "/data/lines",
        "/data/total",
    ]
