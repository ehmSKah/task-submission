"""The per-IP limit through the real path (ADRs 0006 and 0011).

Locally every request comes from the same Docker address, so the preset's IP limit (3 a minute)
is below every user's (10): the IP is refused first, whoever asks.
"""

from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from orders_api.domain.rate_limits import RateLimits, RateLimitScope
from tests.integration.assertions import assert_contract_headers, assert_problem
from tests.integration.rate_limits import (
    assert_rate_limit_headers,
    counts,
    preset_in_force,
    start_from_zero,
)
from tests.integration.requests import bearer

ORDERS = "/api/v1/orders"


@pytest.fixture(scope="module", autouse=True)
def tiny_ip_limits(endpoints: dict[str, Any]) -> Iterator[RateLimits]:
    with preset_in_force(endpoints, "tiny-ip") as limits:
        yield limits


@pytest.fixture(autouse=True)
def _from_zero(endpoints: dict[str, Any], tiny_ip_limits: RateLimits) -> None:
    start_from_zero(endpoints)


def test_the_ip_limit_is_a_429_naming_ip_whoever_asks(
    api: httpx.Client, token_for: Callable[[str], str]
) -> None:
    for user in ("alice", "bob", "manager"):
        assert api.get(ORDERS, headers=bearer(token_for(user))).status_code == 200

    refused = api.get(ORDERS, headers=bearer(token_for("admin")))

    assert_problem(refused, 429, "rate-limited")
    assert refused.json()["scope"] == "ip"
    assert_rate_limit_headers(refused, limit=3, remaining=0)
    assert refused.headers["Retry-After"] == refused.headers["RateLimit-Reset"]


def test_x_forwarded_for_is_never_taken_as_the_client_address(
    api: httpx.Client, token_for: Callable[[str], str], endpoints: dict[str, Any]
) -> None:
    alice = bearer(token_for("alice"))
    forged = ["198.51.100.1", "198.51.100.2", "198.51.100.3", "198.51.100.4"]

    statuses = [
        api.get(ORDERS, headers={**alice, "X-Forwarded-For": address}).status_code
        for address in forged
    ]

    assert statuses == [200, 200, 200, 429]
    [ip_key] = counts(endpoints, RateLimitScope.IP)
    assert not any(address in ip_key for address in forged)


def test_health_is_never_limited(api: httpx.Client, token_for: Callable[[str], str]) -> None:
    alice = bearer(token_for("alice"))
    while api.get(ORDERS, headers=alice).status_code != 429:
        pass

    health = api.get("/api/v1/health")

    assert health.status_code == 200
    assert "RateLimit-Limit" not in health.headers
    assert_contract_headers(health)
