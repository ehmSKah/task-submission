"""The stack as shipped: its own consumer containers and its database roles."""

from uuid import uuid4

import psycopg
import pytest

from tests.integration.stack import (
    CONSUMER_DATABASE_URL,
    assert_nothing_duplicated,
    assert_nothing_lost,
    order_placed_event,
    publish,
)

pytestmark = pytest.mark.integration


def test_the_stacks_consumers_hand_over_events_from_the_orders_topic() -> None:
    order_id = uuid4()

    publish("orders", [(order_id, order_placed_event(order_id))])

    assert_nothing_lost([order_id], topic="orders", group="order-consumer")
    assert_nothing_duplicated([order_id])


def test_the_consumer_role_cannot_change_the_schema() -> None:
    with (
        psycopg.connect(CONSUMER_DATABASE_URL) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        connection.execute("CREATE TABLE intruder (id integer)")


def test_the_consumer_role_cannot_delete_orders() -> None:
    with (
        psycopg.connect(CONSUMER_DATABASE_URL) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        connection.execute("DELETE FROM orders")
