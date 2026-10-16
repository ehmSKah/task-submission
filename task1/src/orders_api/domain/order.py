"""An Order and its line items (ADR 0004; terms in CONTEXT.md)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

INITIAL_VERSION = 1


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
class NewOrder:
    """What a client asks for when creating an order; the API decides everything else."""

    currency: str
    items: tuple[LineItem, ...]


def format_timestamp(moment: datetime) -> str:
    """ISO-8601 UTC with fixed-width milliseconds, so timestamps sort as text (ADR 0005)."""
    utc = moment.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


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

    @classmethod
    def new(cls, *, order_id: str, owner_id: str, request: NewOrder, now: datetime) -> Self:
        """A freshly created order: `PENDING`, version 1, created and updated `now` (ADR 0004)."""
        timestamp = format_timestamp(now)
        return cls(
            id=order_id,
            owner_id=owner_id,
            status=OrderStatus.PENDING,
            currency=request.currency,
            items=request.items,
            created_at=timestamp,
            updated_at=timestamp,
            version=INITIAL_VERSION,
        )

    @property
    def total_amount(self) -> int:
        """Always computed, never supplied by a client."""
        return sum(item.quantity * item.unit_price for item in self.items)
