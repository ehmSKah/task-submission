"""PostgreSQL going down pauses the stack's consumer in place, visibly, until it is back
(ADR 0006, 0007).

Stops and starts the stack's own PostgreSQL, so it runs on its own, after the integration tests.
"""

import subprocess
from uuid import UUID, uuid4

import pytest

from tests.integration.stack import (
    STACK_CONSUMERS,
    Record,
    assert_nothing_duplicated,
    assert_nothing_lost,
    eventually,
    lag,
    publish,
    records,
    wait_until_stable,
)
from tests.reliability.containers import STACK_INSTANCES, TASK2, compose, start_postgres
from tests.support.events import order_placed_event

pytestmark = pytest.mark.reliability

# A paused consumer probes every 5 s with the suite's timings (30 s otherwise); its reconnects
# then take a few seconds more.
RECOVERY_TIMEOUT = 90.0


def make_ps() -> str:
    """What a reviewer sees: the services' health, then each consumer's last health check."""
    return subprocess.run(
        ["make", "--no-print-directory", "-C", str(TASK2), "ps"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def dead_letters_for(order_id: UUID) -> list[Record]:
    return [
        record
        for record in records(STACK_CONSUMERS.dead_letter_topic)
        if record.header("dlt.order-id") == str(order_id)
    ]


def consumer_lines(ps: str) -> list[str]:
    return [line for line in ps.splitlines() if "-consumer-" in line]


def wait_until_healthy() -> None:
    eventually(
        make_ps,
        until=lambda ps: (
            len(consumer_lines(ps)) >= STACK_INSTANCES
            and all("(healthy)" in line for line in consumer_lines(ps) if "Up " in line)
            and "paused" not in ps
        ),
        what="every consumer to be healthy",
        timeout=60.0,
    )


def test_a_consumer_pauses_while_postgres_is_down_and_resumes_when_it_is_back() -> None:
    wait_until_stable(STACK_CONSUMERS, members=STACK_INSTANCES)
    wait_until_healthy()
    order_id = uuid4()
    compose("stop", "postgres")
    try:
        publish(STACK_CONSUMERS.topic, [(order_id, order_placed_event(order_id))])
        paused = eventually(
            make_ps,
            until=lambda ps: (
                "paused (systemic: postgres-unavailable)" in ps
                and any("(unhealthy)" in line for line in consumer_lines(ps))
            ),
            what="`make ps` to show a consumer unhealthy, paused for PostgreSQL",
            timeout=60.0,
        )
        [unhealthy] = [line for line in consumer_lines(paused) if "(unhealthy)" in line]
        assert "Up " in unhealthy, "paused in place, not restarted"
        assert sum(lag(STACK_CONSUMERS).values()) >= 1, "nothing committed"
        assert dead_letters_for(order_id) == [], "nothing dead-lettered"
    finally:
        start_postgres()

    assert_nothing_lost([order_id], STACK_CONSUMERS, timeout=RECOVERY_TIMEOUT)
    assert_nothing_duplicated([order_id])
    assert dead_letters_for(order_id) == []
    wait_until_healthy()
