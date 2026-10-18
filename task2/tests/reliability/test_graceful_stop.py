"""SIGTERM while a stream of orders is being processed (ADR 0007): the instance finishes the
attempt in progress, leaves the group, and the other one takes over at once.

Stops one of the stack's consumer containers, so it runs on its own, after the integration tests.
"""

from collections import Counter
from uuid import uuid4

import pytest

from tests.integration.stack import (
    STACK_CONSUMERS,
    add_fault_rule,
    assert_nothing_duplicated,
    assert_nothing_lost,
    eventually,
    handed_over_count,
    publish,
    wait_until_stable,
)
from tests.reliability.containers import (
    GRACE_PERIOD_SECONDS,
    STACK_INSTANCES,
    consumers,
    exit_code,
    log_lines,
    stop,
)
from tests.support.events import order_placed_event

pytestmark = pytest.mark.reliability

ORDERS = 50
# Slows every handover a little, so the stream is still flowing when the stop arrives.
HANDOVER_DELAY_SECONDS = 0.3
# Leaving the group moves the partitions within a rebalance; an instance that didn't leave would
# keep them until its session timed out (6 s with the suite's timings).
TAKEOVER_SECONDS = 4.0


def test_a_graceful_stop_mid_stream_hands_over_at_once_with_at_most_one_redelivery() -> None:
    wait_until_stable(STACK_CONSUMERS, members=STACK_INSTANCES)
    order_ids = [uuid4() for _ in range(ORDERS)]
    for order_id in order_ids:
        add_fault_rule(order_id, delay_seconds=HANDOVER_DELAY_SECONDS)
    publish(
        STACK_CONSUMERS.topic, [(order_id, order_placed_event(order_id)) for order_id in order_ids]
    )
    eventually(
        lambda: handed_over_count(order_ids),
        until=lambda count: count >= 10,
        what="the stream to be flowing",
    )
    stopped, survivor = consumers()

    took = stop(stopped)

    assert handed_over_count(order_ids) < ORDERS, "the stop must land mid-stream"
    assert took < GRACE_PERIOD_SECONDS, f"the stop took {took:.1f} s"
    assert exit_code(stopped) == 0
    wait_until_stable(STACK_CONSUMERS, members=1, timeout=TAKEOVER_SECONDS)
    assert_nothing_lost(order_ids, STACK_CONSUMERS, timeout=60.0)
    assert_nothing_duplicated(order_ids)
    [stopping] = log_lines(stopped, "consumer.stopping")[-1:]
    assert stopping["paused"] is None
    received = Counter(
        line["order_id"]
        for container in (stopped, survivor)
        for line in log_lines(container, "event.received")
        if line.get("order_id") in {str(order_id) for order_id in order_ids}
    )
    redeliveries = sum(count - 1 for count in received.values())
    assert redeliveries <= 1, f"{redeliveries} redeliveries: {received.most_common(3)}"
