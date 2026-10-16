import uuid
from typing import Any

from orders_api.adapters.outbound.dynamodb_orders import DynamoDbOrderRepository
from tests.builders import an_order
from tests.deployment import store_order


def test_stored_order_reads_back_unchanged(endpoints: dict[str, Any]) -> None:
    order = an_order(id=str(uuid.uuid4()))
    store_order(endpoints, order)

    assert DynamoDbOrderRepository(endpoints["ordersTable"]).get(order.id) == order


def test_missing_order_reads_as_none(endpoints: dict[str, Any]) -> None:
    assert DynamoDbOrderRepository(endpoints["ordersTable"]).get(str(uuid.uuid4())) is None
