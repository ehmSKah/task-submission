"""The reliability suite runs the stack's consumers with the shorter timings of
`compose.test.yaml` (ADR 0009), and leaves every test with the stack as it found it."""

from collections.abc import Iterator

import pytest

from tests.reliability.containers import (
    STACK_FILES,
    STACK_INSTANCES,
    compose,
    consumers,
    docker,
    wait_until_healthy,
)


@pytest.fixture(scope="session", autouse=True)
def suite_timings() -> Iterator[None]:
    """Recreates the consumers with the override, and with the stack's own settings again once
    the suite has run, so a reviewer's stack is left as `make up` made it."""
    compose(
        "up",
        "--detach",
        "--wait",
        "--no-deps",
        "--scale",
        f"consumer={STACK_INSTANCES}",
        "consumer",
    )
    yield
    compose("up", "--detach", "--wait", "--no-deps", "consumer", files=STACK_FILES)


@pytest.fixture(autouse=True)
def stack_restored() -> Iterator[None]:
    """Whatever a test stopped, paused or scaled down runs again, healthy, before the next."""
    yield
    for frozen in consumers(status="paused"):
        docker("unpause", frozen)
    compose("up", "--detach", "--wait", "--no-deps", "--no-recreate", "postgres")
    compose(
        "up",
        "--detach",
        "--no-deps",
        "--no-recreate",
        "--scale",
        f"consumer={STACK_INSTANCES}",
        "consumer",
    )
    wait_until_healthy()
