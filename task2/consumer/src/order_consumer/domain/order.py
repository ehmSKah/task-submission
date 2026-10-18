"""The Order and mapping 1: an order placed event's data becomes an Order (ADR 0001)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class OrderLine:
    sku: str
    quantity: int
    unit_price: int  # minor units of the order's currency

    @property
    def line_total(self) -> int:
        return self.quantity * self.unit_price


@dataclass(frozen=True, slots=True)
class Order:
    order_id: UUID
    customer_id: UUID
    placed_at: datetime  # always UTC
    currency: str
    lines: tuple[OrderLine, ...]
    total: int  # minor units, as stated by the producer

    @property
    def item_count(self) -> int:
        return sum(line.quantity for line in self.lines)


def to_order(data: Mapping[str, Any]) -> Order:
    """Mapping 1. Expects data that has already passed validation."""
    return Order(
        order_id=UUID(data["orderId"]),
        customer_id=UUID(data["customerId"]),
        placed_at=datetime.fromisoformat(data["placedAt"]).astimezone(UTC),
        currency=data["currency"],
        lines=tuple(
            OrderLine(sku=line["sku"], quantity=line["quantity"], unit_price=line["unitPrice"])
            for line in data["lines"]
        ),
        total=data["total"],
    )
