"""Order use cases. They depend only on ports, never on AWS (ADR 0009).

Who may do what follows ADR 0001 and the request pipeline of ADR 0009: the `check_may_*`
functions are the permission step (`403`), run before any data is read and before the
idempotency wrapper; the use cases then load the order and apply visibility (`404`).
"""

from datetime import datetime
from typing import Protocol

from orders_api.domain.order import NewOrder, Order
from orders_api.domain.permissions import Caller, Permission


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


def check_may_create(caller: Caller) -> None:
    """`PermissionDenied` unless the caller may create orders (for themselves)."""
    caller.require(Permission.CREATE)


def check_may_read(caller: Caller) -> None:
    """`PermissionDenied` unless the caller may read any order or at least their own."""
    caller.require(Permission.READ_OWN, Permission.READ_ANY)


def get_order(repository: OrderRepository, caller: Caller, order_id: str) -> Order:
    """The order, if it exists and the caller may see it; otherwise `OrderNotFound` either way,
    so someone else's order is indistinguishable from a missing one (ADR 0001)."""
    order = repository.get(order_id)
    if order is None or not caller.can_see(order):
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
