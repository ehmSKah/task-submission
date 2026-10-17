"""Deletion as a use case: an administrative hard delete in any status (ADRs 0001 and 0004)."""

import pytest

from orders_api.application.orders import OrderNotFound, check_may_delete, delete_order
from orders_api.domain.access import Role
from orders_api.domain.order import OrderStatus
from orders_api.domain.permissions import Caller, PermissionDenied
from tests.builders import an_order
from tests.unit.fakes import InMemoryOrderRepository

UNKNOWN_ID = "0e6bb2c6-0000-4000-8000-000000000000"


def test_only_an_admin_may_delete() -> None:
    check_may_delete(Caller(subject="admin-sub", roles=frozenset({Role.ADMIN})))


@pytest.mark.parametrize("roles", [{Role.USER}, {Role.ORDER_MANAGER}, set()])
def test_everyone_else_is_denied(roles: set[Role]) -> None:
    with pytest.raises(PermissionDenied) as denied:
        check_may_delete(Caller(subject="someone-sub", roles=frozenset(roles)))

    assert str(denied.value) == "Needs one of: order:delete:any"


@pytest.mark.parametrize("status", list(OrderStatus))
def test_an_order_is_deleted_in_any_status(status: OrderStatus) -> None:
    order = an_order(status=status)
    repository = InMemoryOrderRepository([order])

    delete_order(repository, order.id)

    assert repository.get(order.id) is None


def test_an_unknown_order_is_not_found() -> None:
    with pytest.raises(OrderNotFound):
        delete_order(InMemoryOrderRepository(), UNKNOWN_ID)


def test_deleting_twice_is_not_found_the_second_time() -> None:
    order = an_order()
    repository = InMemoryOrderRepository([order])
    delete_order(repository, order.id)

    with pytest.raises(OrderNotFound):
        delete_order(repository, order.id)
