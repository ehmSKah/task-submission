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
    """Mapping 1. Expects data that has already passed validation.

    JSON has one number type, so a schema integer may arrive written as 2.0; `int` changes only
    how it is written, never its value.
    """
    return Order(
        order_id=UUID(data["orderId"]),
        customer_id=UUID(data["customerId"]),
        placed_at=datetime.fromisoformat(data["placedAt"]).astimezone(UTC),
        currency=data["currency"],
        lines=tuple(
            OrderLine(
                sku=line["sku"], quantity=int(line["quantity"]), unit_price=int(line["unitPrice"])
            )
            for line in data["lines"]
        ),
        total=int(data["total"]),
    )


def differing_fields(accepted: Order, incoming: Order) -> list[str]:
    """Where an incoming Order differs from the first accepted version, as JSON Pointers into the
    event (ADR 0002). Only the normalised Order counts: lines are matched by SKU whatever their
    order, and the placement time is compared as an instant."""

    def by_sku(order: Order) -> dict[str, OrderLine]:
        return {line.sku: line for line in order.lines}

    comparisons = (
        ("/data/orderId", accepted.order_id == incoming.order_id),
        ("/data/customerId", accepted.customer_id == incoming.customer_id),
        ("/data/placedAt", accepted.placed_at == incoming.placed_at),
        ("/data/currency", accepted.currency == incoming.currency),
        ("/data/lines", by_sku(accepted) == by_sku(incoming)),
        ("/data/total", accepted.total == incoming.total),
    )
    return [pointer for pointer, same in comparisons if not same]
