import pytest

from orders_api.application.orders import OrderNotFound, get_order
from orders_api.domain.access import Role
from orders_api.domain.permissions import Caller
from tests.builders import an_order
from tests.unit.fakes import InMemoryOrderRepository

ALICE = Caller(subject="alice-sub", roles=frozenset({Role.USER}))
BOB = Caller(subject="bob-sub", roles=frozenset({Role.USER}))
UNKNOWN_ID = "3f0c2f40-9a5e-4b1a-9a43-1d6c2a7b8e10"


def test_returns_the_stored_order_to_its_owner() -> None:
    order = an_order(owner_id="alice-sub")
    repository = InMemoryOrderRepository([order])

    assert get_order(repository, ALICE, order.id) == order


def test_unknown_order_is_not_found() -> None:
    with pytest.raises(OrderNotFound):
        get_order(InMemoryOrderRepository(), ALICE, UNKNOWN_ID)


def test_someone_else_s_order_is_not_found_like_an_unknown_one() -> None:
    order = an_order(owner_id="alice-sub")

    with pytest.raises(OrderNotFound) as foreign:
        get_order(InMemoryOrderRepository([order]), BOB, order.id)

    assert str(foreign.value) == str(OrderNotFound(order.id))


@pytest.mark.parametrize("role", [Role.ORDER_MANAGER, Role.ADMIN])
def test_staff_with_read_any_get_anyone_s_order(role: Role) -> None:
    order = an_order(owner_id="alice-sub")
    staff = Caller(subject="staff-sub", roles=frozenset({role}))

    assert get_order(InMemoryOrderRepository([order]), staff, order.id) == order
