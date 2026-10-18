"""A crash at any point between storing and committing loses nothing and duplicates nothing.

One test per row of ADR 0004's crash table: a consumer instance crashes right after a step, then a
fresh instance in the same group receives the redelivered event (ADR 0007).
"""

from uuid import UUID, uuid4

import pytest

from order_consumer.config import Settings
from tests.integration.in_process import (
    crash_after_handover,
    crash_after_recording,
    crash_after_storing,
    run_until_crash,
    running,
)
from tests.integration.stack import (
    ConsumerGroup,
    assert_nothing_duplicated,
    assert_nothing_lost,
    fulfilment_orders,
    handover_requests,
    lag,
    publish,
    stored_order,
)
from tests.support.events import order_placed_event

pytestmark = pytest.mark.integration


def group_of(settings: Settings) -> ConsumerGroup:
    return ConsumerGroup(topic=settings.kafka_topic, group=settings.kafka_group_id)


def published_order(settings: Settings) -> UUID:
    order_id = uuid4()
    publish(settings.kafka_topic, [(order_id, order_placed_event(order_id))])
    return order_id


def handover_status(order_id: UUID) -> str:
    stored = stored_order(order_id)
    assert stored is not None, f"order {order_id} is not stored"
    return str(stored["handover_status"])


def uncommitted(settings: Settings) -> int:
    return sum(lag(group_of(settings)).values())


def test_a_crash_after_storing_is_completed_by_the_redelivery(
    consumer_settings: Settings,
) -> None:
    order_id = published_order(consumer_settings)

    run_until_crash(consumer_settings, wrap_store=crash_after_storing)
    assert handover_status(order_id) == "awaiting_handover"
    assert handover_requests(order_id) == []
    assert uncommitted(consumer_settings) == 1

    with running(consumer_settings):
        assert_nothing_lost([order_id], group_of(consumer_settings))
    assert_nothing_duplicated([order_id])
    assert handover_requests(order_id) == [201]


def test_a_crash_after_the_handover_is_completed_by_the_replay(
    consumer_settings: Settings,
) -> None:
    order_id = published_order(consumer_settings)

    run_until_crash(consumer_settings, wrap_fulfilment=crash_after_handover)
    assert handover_status(order_id) == "awaiting_handover"
    [created] = fulfilment_orders(order_id)
    assert uncommitted(consumer_settings) == 1

    with running(consumer_settings):
        assert_nothing_lost([order_id], group_of(consumer_settings))
    assert_nothing_duplicated([order_id])
    assert handover_requests(order_id) == [201, 201], "the repeat is answered with the replay"
    stored = stored_order(order_id)
    assert stored is not None
    assert stored["fulfilment_order_id"] == created["id"]


def test_a_crash_after_recording_is_skipped_on_redelivery(consumer_settings: Settings) -> None:
    order_id = published_order(consumer_settings)

    run_until_crash(consumer_settings, wrap_store=crash_after_recording)
    assert handover_status(order_id) == "handed_over"
    assert uncommitted(consumer_settings) == 1

    with running(consumer_settings):
        assert_nothing_lost([order_id], group_of(consumer_settings))
    assert_nothing_duplicated([order_id])
    assert handover_requests(order_id) == [201], "the redelivery has no new effects"
