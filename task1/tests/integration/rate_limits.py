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

from orders_api.adapters.outbound.ssm_rate_limits import rate_limits_to_json
from orders_api.domain.rate_limits import RateLimits
from orders_api_infra.config import load_config
from orders_api_infra.rate_limits import PRESETS, empty_counters, switched_limits, wait_for_window

SECONDS_NEEDED = 15
"""Of the current window, for one test's requests, including a cache wait inside the test."""


@contextmanager
def preset_in_force(endpoints: dict[str, Any], name: str) -> Iterator[RateLimits]:
    with switched_limits(load_config(endpoints["env"]), rate_limits_to_json(PRESETS[name])):
        yield PRESETS[name]


def start_from_zero(endpoints: dict[str, Any]) -> None:
    """A fresh window with enough time left, and no counter in it yet."""
    wait_for_window(SECONDS_NEEDED)
    empty_counters(load_config(endpoints["env"]))


def counts(endpoints: dict[str, Any], scope: str) -> dict[str, int]:
    """The deployed table's counters of one scope (`ip` or `user`), by key."""
    table = boto3.resource("dynamodb").Table(endpoints["rateLimitTable"])
    items = table.scan(ConsistentRead=True)["Items"]
    return {
        str(item["id"]): int(str(item["count"]))
        for item in items
        if str(item["id"]).startswith(f"{scope}#")
    }


def assert_rate_limit_headers(response: httpx.Response, limit: int, remaining: int) -> None:
    assert response.headers["RateLimit-Limit"] == str(limit)
    assert response.headers["RateLimit-Remaining"] == str(remaining)
    assert 1 <= int(response.headers["RateLimit-Reset"]) <= 60
