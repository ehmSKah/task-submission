"""An Order and its line items (ADR 0004; terms in CONTEXT.md)."""

from dataclasses import dataclass
from enum import StrEnum


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class LineItem:
    sku: str
    quantity: int
    unit_price: int
    """Integer minor units of the order's currency (1999 = 19.99)."""


@dataclass(frozen=True)
class Order:
    id: str
    owner_id: str
    """The owner's subject (`sub`) at the identity provider (ADR 0001)."""
    status: OrderStatus
    currency: str
    items: tuple[LineItem, ...]
    created_at: str
    """ISO-8601 UTC with fixed-width milliseconds, so it sorts as text (ADR 0005)."""
    updated_at: str
    version: int

    @property
    def total_amount(self) -> int:
        """Always computed, never supplied by a client."""
        return sum(item.quantity * item.unit_price for item in self.items)
