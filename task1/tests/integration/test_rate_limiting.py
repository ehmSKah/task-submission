"""The per-user limit through the real path, with tiny limits switched in at runtime
(ADRs 0006 and 0011): a USER is refused after 3 requests a minute, staff after 5."""

import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from orders_api.domain.rate_limits import RateLimits, RateLimitScope
from orders_api_infra.config import load_config
from orders_api_infra.rate_limits import switched_limits
from tests.integration.assertions import assert_problem
from tests.integration.function_logs import FunctionLogs, api_function_name
from tests.integration.rate_limits import (
    assert_rate_limit_headers,
    counts,
    preset_in_force,
    start_from_zero,
)
from tests.integration.requests import bearer

ORDERS = "/api/v1/orders"


@pytest.fixture(scope="module", autouse=True)
def tiny_limits(endpoints: dict[str, Any]) -> Iterator[RateLimits]:
    with preset_in_force(endpoints, "tiny") as limits:
        yield limits


@pytest.fixture(autouse=True)
def _from_zero(endpoints: dict[str, Any], tiny_limits: RateLimits) -> None:
    start_from_zero(endpoints)


@pytest.fixture
def alice(token_for: Callable[[str], str]) -> dict[str, str]:
    return bearer(token_for("alice"))


def refused_after_the_limit(api: httpx.Client, headers: dict[str, str]) -> httpx.Response:
    while (response := api.get(ORDERS, headers=headers)).status_code != 429:
        assert response.status_code == 200
    return response


def test_the_user_limit_is_a_429_with_retry_after_the_scope_and_the_headers(
    api: httpx.Client, alice: dict[str, str]
) -> None:
    allowed = [api.get(ORDERS, headers=alice) for _ in range(3)]
    refused = api.get(ORDERS, headers=alice)

    for remaining, response in zip([2, 1, 0], allowed, strict=True):
        assert response.status_code == 200
        assert_rate_limit_headers(response, limit=3, remaining=remaining)
    assert_problem(refused, 429, "rate-limited")
    assert refused.json()["scope"] == "user"
    assert_rate_limit_headers(refused, limit=3, remaining=0)
    assert refused.headers["Retry-After"] == refused.headers["RateLimit-Reset"]


def test_refused_requests_are_not_counted_against_the_user(
    api: httpx.Client,
    alice: dict[str, str],
    subject_for: Callable[[str], str],
    endpoints: dict[str, Any],
) -> None:
    statuses = [api.get(ORDERS, headers=alice).status_code for _ in range(6)]

    assert statuses == [200, 200, 200, 429, 429, 429]
    [(user_key, user_count)] = counts(endpoints, RateLimitScope.USER).items()
    assert user_key.startswith(f"user#{subject_for('alice')}#")
    assert user_count == 3
    # Each was counted against the IP before its user's limit turned it away (ADR 0006).
    assert list(counts(endpoints, RateLimitScope.IP).values()) == [6]


def test_each_role_has_its_tier_and_a_caller_without_one_the_lowest(
    api: httpx.Client, token_for: Callable[[str], str]
) -> None:
    for user, limit in [("alice", 3), ("manager", 5), ("admin", 5), ("nobody", 3)]:
        response = api.get(ORDERS, headers=bearer(token_for(user)))

        assert response.headers["RateLimit-Limit"] == str(limit), user


def test_a_changed_parameter_takes_effect_within_the_cache_time_without_a_deploy(
    api: httpx.Client, alice: dict[str, str], endpoints: dict[str, Any]
) -> None:
    before = api.get(ORDERS, headers=alice)

    with preset_in_force(endpoints, "high"):
        after = api.get(ORDERS, headers=alice)

    assert before.headers["RateLimit-Limit"] == "3"
    assert after.headers["RateLimit-Limit"] == "100000"


def test_an_invalid_value_is_rejected_and_the_last_valid_limits_stay_in_force(
    api: httpx.Client, alice: dict[str, str], endpoints: dict[str, Any]
) -> None:
    """The last valid value is the last one each Lambda instance read, so the function reads the
    tiny limits first (refused requests count as reads too) before the invalid value goes in."""
    config = load_config(endpoints["env"])
    logs = FunctionLogs(api_function_name(endpoints))
    since_ms = int(time.time() * 1000) - 5_000
    before = [api.get(ORDERS, headers=alice) for _ in range(3)]

    with switched_limits(config, '{"perIp": "lots"}'):
        after = [api.get(ORDERS, headers=alice) for _ in range(2)]

    assert [r.headers["RateLimit-Limit"] for r in before + after] == ["3"] * 5
    assert [r.status_code for r in after] == [429, 429]
    logged = logs.wait_for_event("rate_limit_config_invalid", since_ms)
    assert logged["level"] == "ERROR"
    assert logged["parameter"] == config.rate_limits_parameter
    assert "lots" not in str(logged)


def test_the_access_line_of_a_429_names_the_scope_and_only_the_client_s_network(
    api: httpx.Client, alice: dict[str, str], endpoints: dict[str, Any]
) -> None:
    logs = FunctionLogs(api_function_name(endpoints))
    since_ms = int(time.time() * 1000) - 5_000

    refused = refused_after_the_limit(api, alice)

    request_id = refused.headers["X-Request-Id"]
    logs.wait_for(request_id, since_ms)
    [line] = [
        line
        for line in logs.lines(since_ms)
        if line.get("requestId") == request_id and line.get("message") == "request"
    ]
    assert line["status"] == 429
    assert line["rateLimitScope"] == "user"
    assert line["clientNetwork"].endswith(("/24", "/64"))
    [ip_key] = counts(endpoints, RateLimitScope.IP)
    assert ip_key.split("#")[1] not in str(line)
