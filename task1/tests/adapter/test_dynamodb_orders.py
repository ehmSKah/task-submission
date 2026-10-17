import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError

from orders_api.adapters.outbound.dynamodb_orders import DynamoDbOrderRepository
from orders_api.domain.order import Order, OrderStatus
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


# Status changes: one conditional UpdateItem (ADR 0005).

LATER = datetime(2026, 10, 18, 8, 0, tzinfo=UTC)


def stored(endpoints: dict[str, Any], **overrides: object) -> tuple[DynamoDbOrderRepository, Order]:
    repository = DynamoDbOrderRepository(endpoints["ordersTable"])
    order = an_order(id=str(uuid.uuid4()), **overrides)
    repository.add(order)
    return repository, order


@pytest.mark.parametrize("check_version", [False, True], ids=["status-only", "with-version"])
def test_a_status_change_is_stored_and_returned_as_stored(
    endpoints: dict[str, Any], check_version: bool
) -> None:
    repository, order = stored(endpoints, version=4)
    changed = order.with_status(OrderStatus.CONFIRMED, now=LATER)

    result = repository.update_status(order, changed, check_version=check_version)

    assert result == changed
    assert result.version == 5
    assert repository.get(order.id) == changed


def test_a_status_change_from_a_stale_status_writes_nothing(endpoints: dict[str, Any]) -> None:
    repository, order = stored(endpoints, status=OrderStatus.SHIPPED, version=3)
    stale = replace(order, status=OrderStatus.CONFIRMED, version=2)

    result = repository.update_status(
        stale, stale.with_status(OrderStatus.CANCELLED, now=LATER), check_version=False
    )

    assert result is None
    assert repository.get(order.id) == order


def test_a_status_change_from_a_stale_version_writes_nothing_when_checked(
    endpoints: dict[str, Any],
) -> None:
    repository, order = stored(endpoints, version=3)
    stale = replace(order, version=2)

    result = repository.update_status(
        stale, stale.with_status(OrderStatus.CONFIRMED, now=LATER), check_version=True
    )

    assert result is None
    assert repository.get(order.id) == order


def test_a_status_change_never_creates_a_missing_order(endpoints: dict[str, Any]) -> None:
    repository = DynamoDbOrderRepository(endpoints["ordersTable"])
    missing = an_order(id=str(uuid.uuid4()))

    result = repository.update_status(
        missing, missing.with_status(OrderStatus.CONFIRMED, now=LATER), check_version=False
    )

    assert result is None
    assert repository.get(missing.id) is None


@pytest.mark.parametrize("attempt", range(5))
def test_of_simultaneous_conditional_writes_from_one_state_exactly_one_wins(
    endpoints: dict[str, Any], attempt: int
) -> None:
    """Eight writers read the same CONFIRMED order; half ship it, half cancel it."""
    repository, order = stored(endpoints, status=OrderStatus.CONFIRMED, version=2)
    wanted = [OrderStatus.SHIPPED, OrderStatus.CANCELLED] * 4
    start = threading.Barrier(len(wanted))

    def write(status: OrderStatus) -> Order | None:
        writer = DynamoDbOrderRepository(endpoints["ordersTable"])  # its own client
        changed = order.with_status(status, now=LATER)
        start.wait()
        return writer.update_status(order, changed, check_version=False)

    with ThreadPoolExecutor(max_workers=len(wanted)) as pool:
        results = list(pool.map(write, wanted))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].version == 3
    assert repository.get(order.id) == winners[0]


# Deletion: one conditional DeleteItem.


@pytest.mark.parametrize("status", list(OrderStatus))
def test_an_order_is_deleted_in_any_status(endpoints: dict[str, Any], status: OrderStatus) -> None:
    repository, order = stored(endpoints, status=status)

    assert repository.delete(order.id) is True
    assert repository.get(order.id) is None


def test_deleting_a_missing_order_reports_it(endpoints: dict[str, Any]) -> None:
    repository, order = stored(endpoints)
    repository.delete(order.id)

    assert repository.delete(order.id) is False
    assert repository.delete(str(uuid.uuid4())) is False
