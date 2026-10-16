import pytest

from orders_api.application.orders import OrderNotFound, get_order
from tests.builders import an_order
from tests.unit.fakes import InMemoryOrderRepository


def test_returns_the_stored_order() -> None:
    order = an_order()
    repository = InMemoryOrderRepository([order])

    assert get_order(repository, order.id) == order


def test_unknown_order_is_not_found() -> None:
    with pytest.raises(OrderNotFound):
        get_order(InMemoryOrderRepository(), "3f0c2f40-9a5e-4b1a-9a43-1d6c2a7b8e10")
