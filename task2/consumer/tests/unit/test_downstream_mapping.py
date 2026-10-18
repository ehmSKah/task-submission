"""Mapping 2 (ADR 0001, 0003): our Order becomes the fulfilment service's request."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from order_consumer.domain.downstream import to_downstream_request
from order_consumer.domain.money import CURRENCY_EXPONENTS
from order_consumer.domain.order import Order, OrderLine, to_order
from tests.support.strategies import order_data


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

    assert request.payload == {
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

    assert request.payload["total"] == {"amount": "5.00", "currency": "USD"}


def test_currencies_without_minor_units_have_no_decimals() -> None:
    request = to_downstream_request(
        order("JPY", (OrderLine(sku="KNE-ELV-0007", quantity=3, unit_price=1500),), total=4500)
    )

    payload = request.payload
    assert payload["lines"][0]["unitPrice"] == {"amount": "1500", "currency": "JPY"}
    assert payload["total"] == {"amount": "4500", "currency": "JPY"}


def test_the_idempotency_key_is_the_order_id() -> None:
    request = to_downstream_request(
        order("EUR", (OrderLine(sku="KNE-ESC-0042", quantity=1, unit_price=1),), total=1)
    )

    assert request.idempotency_key == "3f1c2a9e-8b4d-4c1e-9f2a-6d7e8f9a0b1c"


def test_currencies_with_three_minor_digits_have_three_decimals() -> None:
    request = to_downstream_request(
        order("KWD", (OrderLine(sku="KNE-ELV-0007", quantity=1, unit_price=5),), total=5)
    )

    assert request.payload["total"] == {"amount": "0.005", "currency": "KWD"}


def written_out(minor_units: int, exponent: int) -> str:
    """The expected decimal string, by placing the decimal point among the digits."""
    digits = str(minor_units).rjust(exponent + 1, "0")
    return f"{digits[:-exponent]}.{digits[-exponent:]}" if exponent else digits


@given(
    currency=st.sampled_from(sorted(CURRENCY_EXPONENTS)),
    unit_price=st.integers(min_value=0, max_value=1_000_000),
    quantity=st.integers(min_value=1, max_value=999),
)
def test_money_is_written_with_its_currencys_exponent(
    currency: str, unit_price: int, quantity: int
) -> None:
    line = OrderLine(sku="KNE-ESC-0042", quantity=quantity, unit_price=unit_price)

    payload = to_downstream_request(order(currency, (line,), total=line.line_total)).payload

    exponent = CURRENCY_EXPONENTS[currency]
    assert payload["lines"][0]["unitPrice"] == {
        "amount": written_out(unit_price, exponent),
        "currency": currency,
    }
    assert payload["total"]["amount"] == written_out(quantity * unit_price, exponent)


@given(data=order_data())
def test_the_requests_total_is_the_sum_of_its_line_totals(data: dict[str, Any]) -> None:
    payload = to_downstream_request(to_order(data)).payload

    line_totals = [Decimal(line["lineTotal"]["amount"]) for line in payload["lines"]]
    assert Decimal(payload["total"]["amount"]) == sum(line_totals)
