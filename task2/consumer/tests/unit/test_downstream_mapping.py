"""Mapping 2 (ADR 0001, 0003): our Order becomes the fulfilment service's request."""

from datetime import UTC, datetime
from uuid import UUID

from order_consumer.domain.downstream import to_downstream_request
from order_consumer.domain.order import Order, OrderLine


def order(currency: str, lines: tuple[OrderLine, ...], total: int) -> Order:
    return Order(
        order_id=UUID("3f1c2a9e-8b4d-4c1e-9f2a-6d7e8f9a0b1c"),
        customer_id=UUID("a9e2b7c4-1d3f-4e5a-8b6c-7d8e9f0a1b2c"),
        placed_at=datetime(2026, 10, 17, 9, 12, 3, tzinfo=UTC),
        currency=currency,
        lines=lines,
        total=total,
    )


def test_request_follows_the_fulfilment_contract() -> None:
    request = to_downstream_request(
        order("EUR", (OrderLine(sku="KNE-ESC-0042", quantity=2, unit_price=1999),), total=3998)
    )

    assert request.as_payload() == {
        "externalReference": "3f1c2a9e-8b4d-4c1e-9f2a-6d7e8f9a0b1c",
        "customerReference": "a9e2b7c4-1d3f-4e5a-8b6c-7d8e9f0a1b2c",
        "orderedAt": "2026-10-17T09:12:03Z",
        "lines": [
            {
                "sku": "KNE-ESC-0042",
                "quantity": 2,
                "unitPrice": {"amount": "19.99", "currency": "EUR"},
                "lineTotal": {"amount": "39.98", "currency": "EUR"},
            }
        ],
        "total": {"amount": "39.98", "currency": "EUR"},
    }


def test_whole_amounts_keep_their_decimals() -> None:
    request = to_downstream_request(
        order("USD", (OrderLine(sku="KNE-ELV-0007", quantity=1, unit_price=500),), total=500)
    )

    assert request.as_payload()["total"] == {"amount": "5.00", "currency": "USD"}


def test_currencies_without_minor_units_have_no_decimals() -> None:
    request = to_downstream_request(
        order("JPY", (OrderLine(sku="KNE-ELV-0007", quantity=3, unit_price=1500),), total=4500)
    )

    payload = request.as_payload()
    assert payload["lines"][0]["unitPrice"] == {"amount": "1500", "currency": "JPY"}
    assert payload["total"] == {"amount": "4500", "currency": "JPY"}


def test_the_idempotency_key_is_the_order_id() -> None:
    request = to_downstream_request(
        order("EUR", (OrderLine(sku="KNE-ESC-0042", quantity=1, unit_price=1),), total=1)
    )

    assert request.idempotency_key == "3f1c2a9e-8b4d-4c1e-9f2a-6d7e8f9a0b1c"
