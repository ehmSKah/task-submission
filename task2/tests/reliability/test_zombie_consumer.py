"""A zombie consumer (ADR 0004, 0007): an instance frozen mid-handover, like a long GC or VM
stall, is evicted from the group, and its partition goes to the other instance, which completes
the order. When the zombie wakes up and finishes, its commit is refused and logged; there is still
one order row and one fulfilment order.

Freezes one of the stack's consumer containers, so it runs on its own, after the integration
tests.
"""

from uuid import uuid4

import pytest

from tests.integration.stack import (
    STACK_CONSUMERS,
    add_fault_rule,
    assert_all_committed,
    assert_nothing_duplicated,
    assert_nothing_lost,
    eventually,
    handover_statuses,
    publish,
    wait_until_handed_over,
    wait_until_stable,
)
from tests.reliability.containers import (
    STACK_INSTANCES,
    consumers,
    docker,
    log_lines,
    partitions_of,
)
from tests.support.events import order_placed_event

pytestmark = pytest.mark.reliability

PARTITION = 0
# Shorter than the consumer's 5 s read timeout (ADR 0003): the freeze lands while the zombie
# waits for the answer.
HANDOVER_DELAY_SECONDS = 4
# The session timeout of 6 s, a rebalance, then the other instance's handover.
TAKEOVER_TIMEOUT = 45.0


def test_a_zombies_commit_fails_and_is_logged_with_one_order_and_one_fulfilment_order() -> None:
    wait_until_stable(STACK_CONSUMERS, members=STACK_INSTANCES)
    [zombie] = [name for name in consumers() if PARTITION in partitions_of(name)]
    order_id = uuid4()
    add_fault_rule(order_id, delay_seconds=HANDOVER_DELAY_SECONDS)
    publish(STACK_CONSUMERS.topic, [(order_id, order_placed_event(order_id))], partition=PARTITION)
    eventually(
        lambda: handover_statuses(order_id),
        until=lambda statuses: statuses == [None],
        what=f"the handover of {order_id} to be in progress",
    )

    docker("pause", zombie)
    try:
        wait_until_stable(STACK_CONSUMERS, members=1, timeout=TAKEOVER_TIMEOUT)
        wait_until_handed_over(order_id, timeout=TAKEOVER_TIMEOUT)
        assert_all_committed(STACK_CONSUMERS)
    finally:
        docker("unpause", zombie)

    [failed] = eventually(
        lambda: [
            line
            for line in log_lines(zombie, "commit.failed")
            if (line["partition"], line["topic"]) == (PARTITION, STACK_CONSUMERS.topic)
        ],
        until=bool,
        what="the zombie to log its refused commit",
    )
    assert failed["level"] == "error"
    assert_nothing_lost([order_id], STACK_CONSUMERS)
    assert_nothing_duplicated([order_id])
    assert len(handover_statuses(order_id)) >= 2, "both instances handed the order over"
