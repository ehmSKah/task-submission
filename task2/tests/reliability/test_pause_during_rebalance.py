"""A rebalance while consumption is paused (ADR 0006, 0007): a message in hand whose instance
stops, or whose partition moves to a newcomer, is left uncommitted, and its new owner processes it
once the outage is over. The pause describes the outage, not the partition, so it outlives the
rebalance.

The outage is PostgreSQL stopping, which pauses every instance that meets it. Stops PostgreSQL and
stops or scales the stack's consumer containers, so it runs on its own, after the integration
tests.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest

from tests.integration.stack import (
    STACK_CONSUMERS,
    assert_nothing_duplicated,
    assert_nothing_lost,
    eventually,
    publish,
    wait_until_stable,
)
from tests.reliability.containers import (
    GRACE_PERIOD_SECONDS,
    STACK_INSTANCES,
    compose,
    consumers,
    exit_code,
    heartbeat,
    log_lines,
    partitions_of,
    scale,
    start_postgres,
    stop,
)
from tests.support.events import order_placed_event

pytestmark = pytest.mark.reliability

PAUSED_FOR_POSTGRES = "systemic: postgres-unavailable"
# The paused instances probe every 5 s with the suite's timings; reconnecting takes a few more.
RECOVERY_TIMEOUT = 90.0


@contextmanager
def postgres_down() -> Iterator[None]:
    compose("stop", "postgres")
    try:
        yield
    finally:
        start_postgres()


def wait_until_paused(container: str) -> None:
    eventually(
        lambda: heartbeat(container),
        until=lambda beat: beat["state"] == "paused" and beat["reason"] == PAUSED_FOR_POSTGRES,
        what=f"{container} to pause for PostgreSQL",
    )


def handed_over_by(container: str, order_id: UUID) -> bool:
    return any(
        line.get("order_id") == str(order_id) for line in log_lines(container, "handover.completed")
    )


def test_a_message_held_by_a_paused_instance_that_stops_is_processed_by_the_other() -> None:
    wait_until_stable(STACK_CONSUMERS, members=STACK_INSTANCES)
    stopped, survivor = consumers()
    order_ids = [uuid4() for _ in range(3)]
    with postgres_down():
        for partition, order_id in enumerate(order_ids):
            publish(
                STACK_CONSUMERS.topic,
                [(order_id, order_placed_event(order_id))],
                partition=partition,
            )
        wait_until_paused(stopped)
        wait_until_paused(survivor)

        took = stop(stopped)

        assert took < GRACE_PERIOD_SECONDS / 2, f"a paused instance stops at once, not {took:.1f} s"
        assert exit_code(stopped) == 0
        [stopping] = log_lines(stopped, "consumer.stopping")[-1:]
        assert stopping["paused"] == PAUSED_FOR_POSTGRES
        held = order_ids[stopping["partition"]]
        wait_until_stable(STACK_CONSUMERS, members=1)
        assert heartbeat(survivor)["state"] == "paused", "the pause outlives the rebalance"

    assert_nothing_lost(order_ids, STACK_CONSUMERS, timeout=RECOVERY_TIMEOUT)
    assert_nothing_duplicated(order_ids)
    assert handed_over_by(survivor, held), "the stopped instance's message went to the survivor"


def test_a_held_message_whose_partition_moves_to_a_newcomer_is_dropped_and_processed_there() -> (
    None
):
    """Which partition cooperative-sticky moves to a newcomer is learnt by a dry run first; the
    order is then published on it, so the move is certain to take the message in hand."""
    scale(1)
    wait_until_stable(STACK_CONSUMERS, members=1)
    [lone] = consumers()
    scale(2)
    wait_until_stable(STACK_CONSUMERS, members=2)
    [moving] = partitions_of(consumers()[1])
    scale(1)
    wait_until_stable(STACK_CONSUMERS, members=1)
    eventually(
        lambda: partitions_of(lone),
        until=lambda held: held == {0, 1, 2},
        what=f"{lone} to hold every partition again",
    )
    order_id = uuid4()
    dropped_before = len(log_lines(lone, "message.dropped"))
    with postgres_down():
        publish(STACK_CONSUMERS.topic, [(order_id, order_placed_event(order_id))], partition=moving)
        wait_until_paused(lone)

        scale(2, wait=False)

        dropped = eventually(
            lambda: log_lines(lone, "message.dropped")[dropped_before:],
            until=bool,
            what=f"{lone} to drop the message in hand",
        )
        assert [(line["partition"], line["reason"]) for line in dropped] == [(moving, "revoked")]
        [_, newcomer] = consumers()
        wait_until_paused(newcomer)

    assert_nothing_lost([order_id], STACK_CONSUMERS, timeout=RECOVERY_TIMEOUT)
    assert_nothing_duplicated([order_id])
    assert handed_over_by(newcomer, order_id), "processed by the partition's new owner"
    assert not handed_over_by(lone, order_id)
