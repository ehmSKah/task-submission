"""Order use cases. They depend only on ports, never on AWS (ADR 0009).

Who may do what follows ADR 0001 and the request pipeline of ADR 0009: the `check_may_*`
functions are the permission step (`403`), run before any data is read and before the
idempotency wrapper; the use cases then load the order and apply visibility (`404`).
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from orders_api.domain.order import NewOrder, Order, OrderStatus
from orders_api.domain.permissions import Caller, Permission, permissions_to_change_to


@dataclass(frozen=True)
class PagePosition:
    """Where a page of a newest-first list ends: the last order's `createdAt` and `id`.

    Lists are ordered by `createdAt`, then `id` (ADR 0004), so this pins a fixed place in the
    list that any store can resume from, whether or not that order still exists.
    """

    created_at: str
    order_id: str


@dataclass(frozen=True)
class OrderPage:
    orders: tuple[Order, ...]
    next_position: PagePosition | None
    """Where the next page starts; `None` once the list is exhausted. A page that ends exactly at
    the last order may still name a position, so the page after it is empty (ADR 0004)."""


class OrderRepository(Protocol):
    def get(self, order_id: str) -> Order | None:
        """Read one order; strongly consistent (ADR 0005)."""
        ...

    def add(self, order: Order) -> None:
        """Store a new order; never overwrites an existing one."""
        ...

    def list_newest_first(
        self, *, owner_id: str | None, limit: int, after: PagePosition | None
    ) -> OrderPage:
        """Up to `limit` orders, newest first, starting after `after` (or at the newest): only
        `owner_id`'s orders, or every order when it is `None`. Eventually consistent (ADR 0005)."""
        ...

    def update_status(self, current: Order, changed: Order, *, check_version: bool) -> Order | None:
        """One atomic conditional write (ADR 0005): `changed`'s status and `updated_at`, with the
        version incremented, only while the stored order still has `current`'s status (and
        `current`'s version, if `check_version`). The order as now stored, or `None` when the
        condition failed because the order changed or disappeared meanwhile."""
        ...

    def delete(self, order_id: str) -> bool:
        """Remove the order for good; `False` if there was none."""
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


def listed_owner(caller: Caller) -> str | None:
    """Whose orders the caller lists: everyone's (`None`) with `order:read:any`, else only their
    own (ADR 0001). `PermissionDenied` if they may read no orders at all."""
    check_may_read(caller)
    return None if Permission.READ_ANY in caller.permissions else caller.subject


def list_orders(
    repository: OrderRepository, caller: Caller, *, limit: int, after: PagePosition | None
) -> OrderPage:
    """One page of the orders the caller may see, newest first (ADR 0004)."""
    return repository.list_newest_first(owner_id=listed_owner(caller), limit=limit, after=after)


class VersionMismatch(Exception):
    """The client's `If-Match` names none of the order's current version: `412` (ADR 0004)."""

    def __init__(self, order_id: str, current_version: int) -> None:
        super().__init__(f"Order {order_id} is at version {current_version}")
        self.order_id = order_id
        self.current_version = current_version


def check_may_change_status(caller: Caller, status: OrderStatus) -> None:
    """`PermissionDenied` unless the caller may request `status` on at least their own orders.

    Cancelling needs `order:cancel:own` or `order:status:any`, anything else `order:status:any`.
    The body names the status, so this runs after body validation and before any data is read.
    """
    caller.require(*permissions_to_change_to(status))


# Each lost race means another status change succeeded. Changes only move forward, so an order
# can win at most three races against one request; the bound only guards against a broken store.
_MAX_WRITE_ATTEMPTS = len(OrderStatus)


def change_order_status(
    repository: OrderRepository,
    clock: Clock,
    caller: Caller,
    order_id: str,
    status: OrderStatus,
    *,
    if_match: Collection[int] | None = None,
) -> Order:
    """Change the order's status with one atomic conditional write, and return it as stored.

    Checks run in ADR 0009's order, after `check_may_change_status`: visibility (`OrderNotFound`;
    a caller who may only cancel their own orders can't tell someone else's from a missing one),
    then `If-Match` (`VersionMismatch`, given the versions the client accepts), then the rules
    (`StatusChangeNotAllowed`). The precondition goes before the rules, as RFC 9110 evaluates
    preconditions before the method: a client whose copy is stale learns that first, even when
    its change would also be refused or is already in effect. Requesting the current status
    writes nothing.

    A write whose condition fails lost a race: the order is read again and everything is decided
    afresh against what the winner left, never overwriting it (ADR 0004).
    """
    order = repository.get(order_id)
    if order is None or not caller.can_change_status_of(order):
        raise OrderNotFound(order_id)
    for _ in range(_MAX_WRITE_ATTEMPTS):
        if if_match is not None and order.version not in if_match:
            raise VersionMismatch(order_id, order.version)
        changed = order.with_status(status, now=clock.now())
        if changed is order:
            return order
        stored = repository.update_status(order, changed, check_version=if_match is not None)
        if stored is not None:
            return stored
        fresh = repository.get(order_id)
        if fresh is None:
            raise OrderNotFound(order_id)
        order = fresh
    raise RuntimeError(f"Order {order_id} kept failing its conditional write")


def check_may_delete(caller: Caller) -> None:
    """`PermissionDenied` unless the caller may delete orders, which only ADMIN may."""
    caller.require(Permission.DELETE_ANY)


def delete_order(repository: OrderRepository, order_id: str) -> None:
    """Hard-delete the order in whatever status (ADR 0004); `OrderNotFound` if there is none.

    `order:delete:any` covers every order, so there is no visibility step.
    """
    if not repository.delete(order_id):
        raise OrderNotFound(order_id)
