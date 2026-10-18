"""The heartbeat file and the health check that reads it (ADR 0007).

The poll loop rewrites the file on every turn, saying by when the next beat is due.
`python -m order_consumer.health` is the Compose healthcheck: healthy only while the instance is
running and its heartbeat is on time; it prints why not otherwise, so `docker compose ps` and
`make ps` show a paused instance and its reason.
"""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

type State = Literal["starting", "running", "paused"]

HEALTH_FILE = Path("/tmp/consumer-health.json")


@dataclass(frozen=True, slots=True)
class Beat:
    state: State  # starting: no assignment yet; running: assigned, possibly nothing
    partitions: tuple[str, ...]  # e.g. ("orders/0", "orders/2")
    due_within: float  # seconds until the next beat is due, at the latest
    reason: str | None = None  # why it is paused, e.g. "systemic: postgres-unavailable"
    error: str | None = None  # what happened, e.g. "401 Unauthorized"


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def heartbeat_content(beat: Beat, now: datetime) -> dict[str, object]:
    return {
        "state": beat.state,
        "reason": beat.reason,
        "error": beat.error,
        "partitions": list(beat.partitions),
        "updatedAt": _utc(now),
        "dueBy": _utc(now + timedelta(seconds=beat.due_within)),
    }


IDLE = "no partitions assigned (more members than partitions)"


def judge(heartbeat: Mapping[str, Any], now: datetime) -> tuple[bool, str]:
    """Healthy or not, and a line saying why."""
    due_by = datetime.fromisoformat(heartbeat["dueBy"])
    if now > due_by:
        return False, f"stale: the heartbeat was due by {_utc(due_by)}, the loop is stuck"
    partitions = ", ".join(heartbeat["partitions"]) or IDLE
    match heartbeat["state"]:
        case "running":
            return True, f"running: {partitions}"
        case "paused":
            return False, f"paused ({heartbeat['reason']}): {heartbeat['error']}; {partitions}"
        case _:
            return False, "starting: waiting for partitions"


class HeartbeatFile:
    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, beat: Beat, now: datetime | None = None) -> None:
        """Replaced in one step, so the check never reads half a file."""
        content = heartbeat_content(beat, now or datetime.now(UTC))
        partial = self._path.with_name(f".{self._path.name}.partial")
        partial.write_text(json.dumps(content, separators=(",", ":")))
        partial.replace(self._path)

    def check(self, now: datetime | None = None) -> tuple[bool, str]:
        try:
            heartbeat = json.loads(self._path.read_text())
            return judge(heartbeat, now or datetime.now(UTC))
        except (OSError, ValueError, KeyError, TypeError) as error:
            return False, f"no heartbeat: {type(error).__name__}: {error}"


def main() -> int:
    healthy, line = HeartbeatFile(Path(os.environ.get("HEALTH_FILE", str(HEALTH_FILE)))).check()
    print(line)
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
