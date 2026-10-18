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
    handover_requests,
    publish,
)
from tests.support.events import order_placed_event

pytestmark = pytest.mark.reliability

COMPOSE_FILE = Path(__file__).parents[2] / "compose.yaml"

# Long enough for the kill to land while the stub is still working on the request.
HANDOVER_DELAY_SECONDS = 10
# A killed instance never leaves the group: its partitions move once its session times out
# (45 s, ADR 0007).
RECOVERY_TIMEOUT = 120.0


def compose(*args: str) -> None:
    """The stack's project comes from COMPOSE_PROJECT_NAME, as for `make`."""
    subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), *args], check=True)


def test_a_consumer_killed_mid_handover_leaves_one_fulfilment_order() -> None:
    order_id = uuid4()
    add_fault_rule(order_id, delay_seconds=HANDOVER_DELAY_SECONDS)

    publish(STACK_CONSUMERS.topic, [(order_id, order_placed_event(order_id))])
    eventually(
        lambda: handover_requests(order_id),
        until=lambda statuses: statuses == [None],
        what=f"the handover of {order_id} to be in progress",
    )
    compose("kill", "consumer")
    assert handover_requests(order_id) == [None], "the kill must land mid-handover"
    compose("start", "consumer")

    assert_nothing_lost([order_id], STACK_CONSUMERS, timeout=RECOVERY_TIMEOUT)
    assert_nothing_duplicated([order_id])
    first, *repeats = handover_requests(order_id)
    assert first == 201, "the stub finished the request the killed consumer made"
    assert repeats, "the restarted consumer handed the order over again"
    assert set(repeats) <= {409, 201}, f"repeats answered with {repeats}"
