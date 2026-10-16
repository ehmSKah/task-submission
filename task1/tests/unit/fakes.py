"""In-memory stand-ins for the ports, so unit tests need no emulator (ADR 0011)."""

from collections.abc import Iterable

from orders_api.domain.order import Order


class InMemoryOrderRepository:
    def __init__(self, orders: Iterable[Order] = ()) -> None:
        self._orders = {order.id: order for order in orders}

    def get(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)
