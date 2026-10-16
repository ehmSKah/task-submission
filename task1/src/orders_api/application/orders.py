"""Order use cases. They depend only on ports, never on AWS (ADR 0009)."""

from datetime import datetime
from typing import Protocol

from orders_api.domain.order import NewOrder, Order


class OrderRepository(Protocol):
    def get(self, order_id: str) -> Order | None:
        """Read one order; strongly consistent (ADR 0005)."""
        ...

    def add(self, order: Order) -> None:
        """Store a new order; never overwrites an existing one."""
        ...


class Clock(Protocol):
    def now(self) -> datetime:
        """The current time, timezone-aware."""
        ...


class IdGenerator(Protocol):
    def new_id(self) -> str:
        """A new order id: a lowercase UUID v4 (ADR 0004)."""
        ...


class OrderNotFound(Exception):
    def __init__(self, order_id: str) -> None:
        super().__init__(f"Order {order_id} not found")
        self.order_id = order_id


def get_order(repository: OrderRepository, order_id: str) -> Order:
    order = repository.get(order_id)
    if order is None:
        raise OrderNotFound(order_id)
    return order


def create_order(
    repository: OrderRepository,
    clock: Clock,
    ids: IdGenerator,
    *,
    owner_id: str,
    request: NewOrder,
) -> Order:
    """Create a `PENDING` order owned by `owner_id`; the total is computed, never accepted."""
    order = Order.new(order_id=ids.new_id(), owner_id=owner_id, request=request, now=clock.now())
    repository.add(order)
    return order
