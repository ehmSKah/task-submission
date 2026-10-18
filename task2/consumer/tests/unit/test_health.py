"""The health check (ADR 0007): healthy only while running with an on-time heartbeat; starting,
paused or overdue is unhealthy, and the check says why."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from order_consumer.health import Beat, HeartbeatFile, heartbeat_content, judge

WRITTEN = datetime(2026, 10, 18, 9, 0, 0, tzinfo=UTC)


def checked(beat: Beat, after: float = 1.0) -> tuple[bool, str]:
    """The beat as written at WRITTEN, checked `after` seconds later."""
    content = json.loads(json.dumps(heartbeat_content(beat, WRITTEN)))
    return judge(content, WRITTEN + timedelta(seconds=after))


def test_a_running_instance_with_an_on_time_heartbeat_is_healthy() -> None:
    assert checked(Beat("running", ("orders/0", "orders/2"), due_within=30.0)) == (
        True,
        "running: orders/0, orders/2",
    )


def test_an_instance_given_no_partitions_is_healthy_and_says_it_is_idle() -> None:
    assert checked(Beat("running", (), due_within=30.0)) == (
        True,
        "running: no partitions assigned (more members than partitions)",
    )


def test_a_starting_instance_is_not_yet_healthy() -> None:
    """So `docker compose up --wait` returns only once the consumers hold partitions."""
    assert checked(Beat("starting", (), due_within=30.0)) == (
        False,
        "starting: waiting for partitions",
    )


def test_a_paused_instance_is_unhealthy_with_its_reason() -> None:
    beat = Beat("paused", ("orders/1",), 30.0, "systemic: fulfilment", "401 Unauthorized")

    assert checked(beat) == (False, "paused (systemic: fulfilment): 401 Unauthorized; orders/1")


def test_a_heartbeat_past_its_due_time_is_unhealthy() -> None:
    healthy, line = checked(Beat("running", ("orders/0",), due_within=30.0), after=31.0)

    assert not healthy
    assert line == "stale: the heartbeat was due by 2026-10-18T09:00:30.000Z, the loop is stuck"


def test_a_busy_loop_is_healthy_until_its_processing_budget_runs_out() -> None:
    busy = Beat("running", ("orders/0",), due_within=140.0)

    assert checked(busy, after=139.0)[0]
    assert not checked(busy, after=141.0)[0]


def test_a_missing_or_unreadable_heartbeat_file_is_unhealthy(tmp_path: Path) -> None:
    missing = HeartbeatFile(tmp_path / "missing.json")
    garbled = tmp_path / "garbled.json"
    garbled.write_text("{")

    assert missing.check(WRITTEN)[0] is False
    assert HeartbeatFile(garbled).check(WRITTEN)[0] is False


def test_the_heartbeat_file_holds_what_was_last_written(tmp_path: Path) -> None:
    heartbeat = HeartbeatFile(tmp_path / "health.json")

    heartbeat.write(Beat("starting", (), due_within=30.0), WRITTEN)
    heartbeat.write(Beat("running", ("orders/2",), due_within=30.0), WRITTEN)

    assert heartbeat.check(WRITTEN) == (True, "running: orders/2")
    assert json.loads((tmp_path / "health.json").read_text()) == {
        "state": "running",
        "reason": None,
        "error": None,
        "partitions": ["orders/2"],
        "updatedAt": "2026-10-18T09:00:00.000Z",
        "dueBy": "2026-10-18T09:00:30.000Z",
    }
    assert list(tmp_path.iterdir()) == [tmp_path / "health.json"], "no temporary file left"
