"""Shared pieces of the rate-limit tests (ADRs 0006 and 0011).

Each rate-limit module switches in a tiny preset for its tests and the session's high limits
come back afterwards, also when a test fails. Every test starts from an empty counter table with
enough of the current window left for its requests.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import boto3
import httpx

from orders_api.adapters.outbound.dynamodb_rate_limits import COUNT_ATTRIBUTE, KEY_ATTRIBUTE
from orders_api.domain.rate_limits import RateLimits, RateLimitScope
from orders_api_infra import rate_limits as limit_tools
from orders_api_infra.config import load_config

SECONDS_NEEDED = 15
"""Of the current window, for one test's requests, including a cache wait inside the test."""


@contextmanager
def preset_in_force(endpoints: dict[str, Any], name: str) -> Iterator[RateLimits]:
    """Preset `name` in force for a module's tests; the session's limits come back afterwards."""
    with limit_tools.preset_in_force(load_config(endpoints["env"]), name) as limits:
        yield limits


def start_from_zero(endpoints: dict[str, Any]) -> None:
    """A fresh window with enough time left, and no counter in it yet."""
    limit_tools.wait_for_window(SECONDS_NEEDED)
    limit_tools.empty_counters(load_config(endpoints["env"]))


def counts(endpoints: dict[str, Any], scope: RateLimitScope) -> dict[str, int]:
    """The deployed table's counters of one scope, by key."""
    table = boto3.resource("dynamodb").Table(endpoints["rateLimitTable"])
    items = table.scan(ConsistentRead=True)["Items"]
    return {
        str(item[KEY_ATTRIBUTE]): int(str(item[COUNT_ATTRIBUTE]))
        for item in items
        if str(item[KEY_ATTRIBUTE]).startswith(f"{scope.value}#")
    }


def assert_rate_limit_headers(response: httpx.Response, limit: int, remaining: int) -> None:
    assert response.headers["RateLimit-Limit"] == str(limit)
    assert response.headers["RateLimit-Remaining"] == str(remaining)
    assert 1 <= int(response.headers["RateLimit-Reset"]) <= 60
