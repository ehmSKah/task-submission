"""Listing orders: who sees what, newest first, page by page (ADRs 0001 and 0004)."""

import pytest

from orders_api.application.orders import (
    OrderRepository,
    PagePosition,
    list_orders,
    listed_owner,
)
from orders_api.domain.access import Role
from orders_api.domain.order import Order
from orders_api.domain.permissions import Caller, PermissionDenied
from tests.builders import an_order
from tests.unit.fakes import InMemoryOrderRepository

ALICE = Caller(subject="alice-sub", roles=frozenset({Role.USER}))
MANAGER = Caller(subject="manager-sub", roles=frozenset({Role.ORDER_MANAGER}))
NOBODY = Caller(subject="nobody-sub", roles=frozenset())


def order(n: int, owner: str = "alice-sub", created_at: str | None = None) -> Order:
    """The n-th order: later n, later `createdAt` (unless given)."""
    return an_order(
        id=f"00000000-0000-4000-8000-{n:012d}",
        owner_id=owner,
        created_at=created_at or f"2026-10-17T09:30:{n:02d}.000Z",
    )


def walk(repository: OrderRepository, caller: Caller, limit: int) -> list[list[Order]]:
    """Every page, following positions until there is none."""
    pages, after = [], None
    while True:
        page = list_orders(repository, caller, limit=limit, after=after)
        pages.append(list(page.orders))
        if page.next_position is None:
            return pages
        after = page.next_position


def test_a_user_lists_only_their_own_orders_newest_first() -> None:
    repository = InMemoryOrderRepository([order(1), order(2, "bob-sub"), order(3)])

    page = list_orders(repository, ALICE, limit=10, after=None)

    assert page.orders == (order(3), order(1))
    assert page.next_position is None


@pytest.mark.parametrize("role", [Role.ORDER_MANAGER, Role.ADMIN])
def test_staff_with_read_any_list_everyone_s_orders(role: Role) -> None:
    staff = Caller(subject="staff-sub", roles=frozenset({role}))
    repository = InMemoryOrderRepository([order(1), order(2, "bob-sub")])

    assert list_orders(repository, staff, limit=10, after=None).orders == (
        order(2, "bob-sub"),
        order(1),
    )


def test_listed_owner_is_the_scope_of_the_caller_s_list() -> None:
    assert listed_owner(ALICE) == "alice-sub"
    assert listed_owner(MANAGER) is None


def test_a_caller_without_any_read_permission_lists_nothing() -> None:
    with pytest.raises(PermissionDenied):
        list_orders(InMemoryOrderRepository([order(1, "nobody-sub")]), NOBODY, limit=10, after=None)


def test_walking_the_pages_returns_every_order_once_newest_first() -> None:
    orders = [order(n) for n in range(1, 8)]
    repository = InMemoryOrderRepository(orders)

    pages = walk(repository, ALICE, limit=3)

    assert [len(page) for page in pages] == [3, 3, 1]
    assert [o for page in pages for o in page] == orders[::-1]


def test_a_page_ending_at_the_last_order_is_followed_by_an_empty_last_page() -> None:
    repository = InMemoryOrderRepository([order(n) for n in range(1, 5)])

    assert [len(page) for page in walk(repository, ALICE, limit=2)] == [2, 2, 0]


def test_equal_timestamps_are_ordered_by_id_so_pages_never_repeat_or_skip() -> None:
    same_moment = "2026-10-17T09:30:00.000Z"
    orders = [order(n, created_at=same_moment) for n in range(1, 6)]
    repository = InMemoryOrderRepository(orders[::2] + orders[1::2])

    pages = walk(repository, ALICE, limit=2)

    assert [o for page in pages for o in page] == sorted(orders, key=lambda o: o.id, reverse=True)


def test_orders_created_during_a_walk_are_not_part_of_it() -> None:
    repository = InMemoryOrderRepository([order(n) for n in range(1, 5)])
    first = list_orders(repository, ALICE, limit=2, after=None)
    repository.add(order(9))

    rest = list_orders(repository, ALICE, limit=10, after=first.next_position)

    assert first.orders + rest.orders == (order(4), order(3), order(2), order(1))


def test_a_position_survives_the_deletion_of_the_order_it_names() -> None:
    repository = InMemoryOrderRepository([order(n) for n in range(1, 4)])
    gone = PagePosition(created_at=order(5).created_at, order_id=order(5).id)

    assert list_orders(repository, ALICE, limit=10, after=gone).orders == (
        order(3),
        order(2),
        order(1),
    )
