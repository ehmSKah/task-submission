"""Both list indexes against the emulator: newest first, stable pages (ADRs 0004 and 0005).

The table outlives test runs and other tests write to it too, so every test uses owners of its
own and checks the orders it stored, never absolute counts. Timestamps are taken from just
before now, so these orders sink into every-owner lists like any other and never sit on top.
"""

import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from orders_api.adapters.outbound.dynamodb_orders import DynamoDbOrderRepository
from orders_api.application.orders import OrderPage, PagePosition
from orders_api.domain.order import Order, format_timestamp
from tests.builders import an_order
from tests.polling import eventually

START = datetime.now(UTC) - timedelta(minutes=1)


@pytest.fixture
def repository(endpoints: dict[str, Any]) -> DynamoDbOrderRepository:
    return DynamoDbOrderRepository(endpoints["ordersTable"])


def stored(repository: DynamoDbOrderRepository, owner: str, *offsets_ms: int) -> list[Order]:
    """One order per offset, created that many milliseconds after `START`, added in a shuffled
    order; returned newest first, as a list shows them."""
    orders = [
        an_order(
            id=str(uuid.uuid4()),
            owner_id=owner,
            created_at=format_timestamp(START + timedelta(milliseconds=offset)),
        )
        for offset in offsets_ms
    ]
    for order in random.sample(orders, len(orders)):
        repository.add(order)
    return newest_first(orders)


def newest_first(orders: list[Order]) -> list[Order]:
    return sorted(orders, key=lambda o: (o.created_at, o.id), reverse=True)


def a_new_owner() -> str:
    return f"owner-{uuid.uuid4()}"


def walk(repository: DynamoDbOrderRepository, owner_id: str | None, limit: int) -> list[OrderPage]:
    pages, after = [], None
    while True:
        page = repository.list_newest_first(owner_id=owner_id, limit=limit, after=after)
        pages.append(page)
        if page.next_position is None:
            return pages
        after = page.next_position


def orders_of(pages: list[OrderPage]) -> list[Order]:
    return [order for page in pages for order in page.orders]


def walked_as(
    repository: DynamoDbOrderRepository, owner: str, orders: list[Order], limit: int = 100
) -> list[OrderPage]:
    """The owner's pages, once they show exactly `orders`."""

    def walked() -> list[OrderPage]:
        pages = walk(repository, owner, limit)
        assert orders_of(pages) == orders
        return pages

    return eventually(walked)


def test_by_owner_lists_one_owner_s_orders_newest_first(
    repository: DynamoDbOrderRepository,
) -> None:
    owner = a_new_owner()
    mine = stored(repository, owner, 30, 10, 50, 20, 40)
    stored(repository, a_new_owner(), 15, 25)

    pages = walked_as(repository, owner, mine, limit=2)

    assert [len(page.orders) for page in pages] == [2, 2, 1]


def test_a_full_last_page_is_followed_by_an_empty_one(
    repository: DynamoDbOrderRepository,
) -> None:
    """DynamoDB names a next position whenever `Limit` is reached, even with nothing after it."""
    owner = a_new_owner()
    mine = stored(repository, owner, 1, 2, 3, 4)

    pages = walked_as(repository, owner, mine, limit=2)

    assert [len(page.orders) for page in pages] == [2, 2, 0]
    assert pages[1].next_position == PagePosition(
        created_at=mine[-1].created_at, order_id=mine[-1].id
    )


def test_equal_timestamps_are_ordered_by_id_so_pages_never_repeat_or_skip(
    repository: DynamoDbOrderRepository,
) -> None:
    owner = a_new_owner()
    mine = stored(repository, owner, 7, 7, 7, 7, 7)

    walked_as(repository, owner, mine, limit=2)

    assert len({order.created_at for order in mine}) == 1


def test_orders_added_during_a_walk_are_not_part_of_it(
    repository: DynamoDbOrderRepository,
) -> None:
    owner = a_new_owner()
    mine = stored(repository, owner, 1, 2, 3, 4)
    walked_as(repository, owner, mine)

    first = repository.list_newest_first(owner_id=owner, limit=2, after=None)
    newer = stored(repository, owner, 5, 9)
    walked_as(repository, owner, newer + mine)
    rest = repository.list_newest_first(owner_id=owner, limit=10, after=first.next_position)

    assert list(first.orders + rest.orders) == mine


def test_a_position_resumes_even_when_its_order_no_longer_exists(
    repository: DynamoDbOrderRepository,
) -> None:
    """A deleted order's position still marks a place in the index (ADR 0004)."""
    owner = a_new_owner()
    mine = stored(repository, owner, 10, 20, 30)
    walked_as(repository, owner, mine)
    gone = PagePosition(
        created_at=format_timestamp(START + timedelta(milliseconds=25)),
        order_id=str(uuid.uuid4()),
    )

    page = repository.list_newest_first(owner_id=owner, limit=10, after=gone)

    assert list(page.orders) == mine[1:]


def test_by_created_lists_every_owner_s_orders_newest_first(
    repository: DynamoDbOrderRepository,
) -> None:
    """Walked until all of this test's orders are seen; the table holds others' orders too."""
    mine = newest_first(
        stored(repository, a_new_owner(), 3, 5, 5) + stored(repository, a_new_owner(), 4, 5, 6)
    )
    wanted = {order.id for order in mine}

    def walked_to_mine() -> list[Order]:
        seen: list[Order] = []
        after = None
        while True:
            page = repository.list_newest_first(owner_id=None, limit=25, after=after)
            seen += page.orders
            if wanted <= {order.id for order in seen}:
                return seen
            assert page.next_position is not None, "walked to the end without seeing them all"
            after = page.next_position

    seen = eventually(walked_to_mine)

    keys = [(order.created_at, order.id) for order in seen]
    assert keys == sorted(set(keys), reverse=True)
    assert [order for order in seen if order.id in wanted] == mine
    assert len({order.owner_id for order in mine}) == 2
