"""A second instance joining while one is working through a backlog (ADR 0007): the partitions
split 2 + 1, and nothing is lost or duplicated.

Scales the stack's consumer containers, so it runs on its own, after the integration tests.
"""

from uuid import uuid4

import pytest

from tests.integration.stack import (
    STACK_CONSUMERS,
    add_fault_rule,
    assert_nothing_duplicated,
    assert_nothing_lost,
    describe_group,
    eventually,
    handed_over_count,
    publish,
    wait_until_stable,
)
from tests.reliability.containers import scale
from tests.support.events import order_placed_event

pytestmark = pytest.mark.reliability

ORDERS = 50
# One instance takes about 25 s for the backlog, so the second joins well before the end.
HANDOVER_DELAY_SECONDS = 0.5


def test_scaling_from_one_to_two_instances_under_load_splits_the_partitions_two_and_one() -> None:
    scale(1)
    wait_until_stable(STACK_CONSUMERS, members=1)
    order_ids = [uuid4() for _ in range(ORDERS)]
    for order_id in order_ids:
        add_fault_rule(order_id, delay_seconds=HANDOVER_DELAY_SECONDS)
    publish(
        STACK_CONSUMERS.topic, [(order_id, order_placed_event(order_id)) for order_id in order_ids]
    )
    eventually(
        lambda: handed_over_count(order_ids),
        until=lambda count: count >= ORDERS // 4,
        what="a quarter of the backlog to be handed over",
    )

    scale(2, wait=False)

    assert handed_over_count(order_ids) < ORDERS // 2, "the second instance must join under load"
    wait_until_stable(STACK_CONSUMERS, members=2)
    split = sorted(
        len(member.assignment.topic_partitions)
        for member in describe_group(STACK_CONSUMERS).members
    )
    assert split == [1, 2]
    assert_nothing_lost(order_ids, STACK_CONSUMERS, timeout=60.0)
    assert_nothing_duplicated(order_ids)
