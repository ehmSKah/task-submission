"""Order use cases. They depend only on ports, never on AWS (ADR 0009)."""

from typing import Protocol

from orders_api.domain.order import Order


class OrderRepository(Protocol):
    def get(self, order_id: str) -> Order | None:
        """Read one order; strongly consistent (ADR 0005)."""
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
