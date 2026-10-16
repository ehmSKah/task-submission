"""In-memory stand-ins for the ports, so unit tests need no emulator (ADR 0011)."""

from collections.abc import Iterable
from datetime import datetime

from orders_api.domain.order import Order


class InMemoryOrderRepository:
    def __init__(self, orders: Iterable[Order] = ()) -> None:
        self._orders = {order.id: order for order in orders}

    def get(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def add(self, order: Order) -> None:
        assert order.id not in self._orders, f"order {order.id} already exists"
        self._orders[order.id] = order


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class SequentialIds:
    def __init__(self, ids: Iterable[str]) -> None:
        self._ids = iter(ids)

    def new_id(self) -> str:
        return next(self._ids)
