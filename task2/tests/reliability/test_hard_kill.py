"""A consumer killed in the middle of a handover, then started again (ADR 0007).

Drives the stack's own consumer containers on the real `orders` topic, so it runs on its own,
after the integration tests.
"""

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from tests.integration.stack import (
    STACK_CONSUMERS,
    add_fault_rule,
    assert_nothing_duplicated,
    assert_nothing_lost,
    eventually,
    handover_statuses,
    publish,
    wait_until_stable,
)
from tests.support.events import order_placed_event

pytestmark = pytest.mark.reliability

COMPOSE_FILE = Path(__file__).parents[2] / "compose.yaml"

# Shorter than the consumer's 5 s read timeout (ADR 0003): while the stub still holds the request,
# the consumer that sent it is still waiting for the answer, so a kill then lands mid-handover.
HANDOVER_DELAY_SECONDS = 4
# A killed instance never leaves the group: its partitions move only once its session has timed
# out (45 s, ADR 0007).
RECOVERY_TIMEOUT = 120.0
STACK_INSTANCES = 2


def compose(*args: str) -> None:
    """The stack's project comes from COMPOSE_PROJECT_NAME, as for `make`."""
    subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), *args], check=True)


def test_consumers_killed_mid_handover_leave_one_fulfilment_order() -> None:
    order_id = uuid4()
    add_fault_rule(order_id, delay_seconds=HANDOVER_DELAY_SECONDS)
    killed = wait_until_stable(STACK_CONSUMERS, members=STACK_INSTANCES)

    publish(STACK_CONSUMERS.topic, [(order_id, order_placed_event(order_id))])
    eventually(
        lambda: handover_statuses(order_id),
        until=lambda statuses: statuses == [None],
        what=f"the handover of {order_id} to be in progress",
    )
    compose("kill", "consumer")
    assert handover_statuses(order_id) == [None], "the kill must land mid-handover"
    compose("start", "consumer")
    wait_until_stable(
        STACK_CONSUMERS, members=STACK_INSTANCES, replacing=killed, timeout=RECOVERY_TIMEOUT
    )

    assert_nothing_lost([order_id], STACK_CONSUMERS)
    assert_nothing_duplicated([order_id])
    first, *repeats = handover_statuses(order_id)
    assert first == 201, "the stub finished the request the killed consumer made"
    assert repeats, "the restarted consumer handed the order over again"
    assert set(repeats) <= {409, 201}, f"repeats answered with {repeats}"
