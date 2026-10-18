"""Driving the stack's consumer containers (ADR 0007, 0009).

Every Compose command layers `compose.test.yaml` on `compose.yaml`, so containers the tests scale,
start or recreate keep the suite's shorter timings. The stack's project comes from
COMPOSE_PROJECT_NAME, as for `make`.
"""

import json
import subprocess
import time
from pathlib import Path
from typing import Any

TASK2 = Path(__file__).parents[2]
STACK_FILES = ("-f", str(TASK2 / "compose.yaml"))
TEST_FILES = (*STACK_FILES, "-f", str(TASK2 / "compose.test.yaml"))
STACK_INSTANCES = 2
# compose.yaml's stop_grace_period: a stop taking longer would end in a SIGKILL.
GRACE_PERIOD_SECONDS = 20.0


def compose(*args: str, files: tuple[str, ...] = TEST_FILES) -> str:
    return subprocess.run(
        ["docker", "compose", *files, *args], check=True, capture_output=True, text=True
    ).stdout


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout


def scale(instances: int, wait: bool = True) -> None:
    """Adds containers, or removes the newest; the others stay as they are. Without `wait`, for
    a newcomer that is expected to pause, and so never to become healthy."""
    args = ["up", "--detach", "--no-deps", "--no-recreate", "--scale", f"consumer={instances}"]
    compose(*args, *(["--wait"] if wait else []), "consumer")


def consumers(status: str = "running") -> list[str]:
    """The consumer containers' names in this state, oldest first."""
    names = compose("ps", "--status", status, "--format", "{{.Name}}", "consumer").split()
    return sorted(names, key=lambda name: int(name.rsplit("-", 1)[1]))


def stop(container: str) -> float:
    """Sends SIGTERM and waits until the container has exited; returns how long that took."""
    started = time.monotonic()
    docker("stop", container)
    return time.monotonic() - started


def exit_code(container: str) -> int:
    return int(docker("inspect", "--format", "{{.State.ExitCode}}", container))


def heartbeat(container: str) -> dict[str, Any]:
    content: dict[str, Any] = json.loads(
        docker("exec", container, "cat", "/tmp/consumer-health.json")
    )
    return content


def partitions_of(container: str) -> set[int]:
    """The partitions the instance's heartbeat says it holds."""
    return {int(label.rsplit("/", 1)[1]) for label in heartbeat(container)["partitions"]}


def log_lines(container: str, event: str | None = None) -> list[dict[str, Any]]:
    """The container's JSON log lines, every run of it, optionally only one event's."""
    result = subprocess.run(
        ["docker", "logs", container], check=True, capture_output=True, text=True
    )
    lines = []
    for raw in (result.stdout + result.stderr).splitlines():
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(line, dict) and (event is None or line.get("event") == event):
            lines.append(line)
    return lines
