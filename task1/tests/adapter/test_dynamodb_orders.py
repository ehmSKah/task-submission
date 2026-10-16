import uuid
from dataclasses import replace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from orders_api.adapters.outbound.dynamodb_orders import DynamoDbOrderRepository
from tests.builders import an_order
from tests.deployment import store_order


def test_stored_order_reads_back_unchanged(endpoints: dict[str, Any]) -> None:
    order = an_order(id=str(uuid.uuid4()))
    store_order(endpoints, order)

    assert DynamoDbOrderRepository(endpoints["ordersTable"]).get(order.id) == order


def test_missing_order_reads_as_none(endpoints: dict[str, Any]) -> None:
    assert DynamoDbOrderRepository(endpoints["ordersTable"]).get(str(uuid.uuid4())) is None


def test_added_order_reads_back_unchanged(endpoints: dict[str, Any]) -> None:
    repository = DynamoDbOrderRepository(endpoints["ordersTable"])
    order = an_order(id=str(uuid.uuid4()))

    repository.add(order)

    assert repository.get(order.id) == order


def test_add_never_replaces_an_existing_order(endpoints: dict[str, Any]) -> None:
    repository = DynamoDbOrderRepository(endpoints["ordersTable"])
    order = an_order(id=str(uuid.uuid4()))
    repository.add(order)

    with pytest.raises(ClientError, match="ConditionalCheckFailed"):
        repository.add(replace(order, owner_id="mallory-sub"))

    assert repository.get(order.id) == order
