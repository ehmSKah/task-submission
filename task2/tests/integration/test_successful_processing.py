"""The assignment's first test category: successful processing (ADR 0009)."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tests.integration.stack import (
    ConsumerGroup,
    assert_nothing_duplicated,
    assert_nothing_lost,
    fulfilment_orders,
    publish,
    stored_order,
)
from tests.support.events import order_placed_envelope, order_placed_event

pytestmark = pytest.mark.integration


def test_a_valid_event_is_stored_handed_over_and_committed(consumer: ConsumerGroup) -> None:
    order_id = uuid4()

    publish(consumer.topic, [(order_id, order_placed_event(order_id))])

    assert_nothing_lost([order_id], consumer)
    assert_nothing_duplicated([order_id])
    stored = stored_order(order_id)
    assert stored is not None
    [fulfilment_order] = fulfilment_orders(order_id)
    assert stored["fulfilment_order_id"] == fulfilment_order["id"]
    assert stored["placed_at"] == datetime(2026, 10, 17, 9, 12, 3, tzinfo=UTC)
    assert (stored["total"], stored["item_count"]) == (4498, 3)


def test_several_events_across_partitions_are_all_handed_over_once(
    consumer: ConsumerGroup,
) -> None:
    order_ids = [uuid4() for _ in range(10)]

    publish(consumer.topic, [(order_id, order_placed_event(order_id)) for order_id in order_ids])

    assert_nothing_lost(order_ids, consumer)
    assert_nothing_duplicated(order_ids)


def test_unknown_fields_inside_v1_are_ignored_and_not_handed_over(consumer: ConsumerGroup) -> None:
    """The stub rejects fields outside the fulfilment contract, so a handover proves they were
    dropped and the request had the fulfilment service's own shape."""
    order_id = uuid4()
    lines = [{"sku": "KNE-ESC-0042", "quantity": 2, "unitPrice": 2249, "giftWrap": True}]
    envelope = order_placed_envelope(order_id, promoCode="AUTUMN", lines=lines)
    envelope["partitionkey"] = "an extension attribute"

    publish(consumer.topic, [(order_id, json.dumps(envelope).encode())])

    assert_nothing_lost([order_id], consumer)
    assert_nothing_duplicated([order_id])
