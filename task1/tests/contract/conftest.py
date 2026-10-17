"""Contract tests run Schemathesis over HTTP against the deployed stack (ADRs 0008 and 0011)."""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
import schemathesis
from schemathesis.schemas import BaseSchema

from orders_api_infra.config import load_config
from orders_api_infra.openapi_extensions import load_spec
from orders_api_infra.rate_limits import preset_in_force
from tests.contract.harness import (
    CONFIG,
    StatusesSeen,
    bearer_auth_provider,
    config_with_known_orders,
    known_orders,
)
from tests.deployment import store_order
from tests.integration.requests import subject_of

# Who the generated requests are sent as. ADMIN holds every permission, so each operation reaches
# its success: deletes included, and status changes to any status. A USER lists through the other
# index, may only cancel their own orders, and meets the 403s and someone else's order as a 404.
CALLERS = ("admin", "alice")

_statuses_seen = StatusesSeen()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.contract)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    lines = _statuses_seen.lines()
    if lines:
        terminalreporter.section("statuses seen per caller and operation")
        for line in lines:
            terminalreporter.write_line(line)


@pytest.fixture(scope="session", autouse=True)
def high_rate_limits(endpoints: dict[str, Any]) -> Iterator[None]:
    """Thousands of requests per caller from one address: the rate limits mustn't decide the
    results. ADR 0006's defaults are in force again afterwards, as after the integration suite."""
    with preset_in_force(load_config(endpoints["env"]), "high", afterwards="defaults"):
        yield


@pytest.fixture(scope="session")
def statuses_seen() -> StatusesSeen:
    return _statuses_seen


@pytest.fixture(params=CALLERS)
def caller(request: pytest.FixtureRequest) -> str:
    """The demo user the generated requests are sent as."""
    name: str = request.param
    return name


@pytest.fixture
def api_schema(
    caller: str, endpoints: dict[str, Any], token_for: Callable[[str], str]
) -> BaseSchema:
    """The contract as Schemathesis tests it, sent as `caller`, with the known orders written
    afresh, so each caller starts from the same state whatever the previous one deleted."""
    owner, other = (subject_of(token_for(user)) for user in ("alice", "bob"))
    for order in known_orders(owner=owner, other=other):
        store_order(endpoints, order)
    config = config_with_known_orders(CONFIG.read_text())
    schema = schemathesis.openapi.from_dict(load_spec(), config=config)
    schema.config.update(base_url=endpoints["apiUrl"])
    # The token is fetched once per session and lasts longer than the run.
    schema.auth(refresh_interval=None)(bearer_auth_provider(lambda: token_for(caller)))
    return schema
