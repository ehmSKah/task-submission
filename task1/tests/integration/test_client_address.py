"""The client address behind the edge-optimized endpoint, and the per-IP limit counting it (ADRs
0006 and 0011). AWS only: locally every request comes from the same Docker address.

The address the API function counts is read from the counter table's `ip#…` key, which holds it
in full, and compared with the client's public address from an IP echo service.
"""

from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from orders_api.domain.rate_limits import RateLimits, RateLimitScope, ip_subject
from tests.integration.assertions import assert_problem
from tests.integration.contract import contract_checked_client
from tests.integration.public_address import ipv4_transport, public_ipv4
from tests.integration.rate_limits import counts, preset_in_force, start_from_zero
from tests.integration.requests import bearer

pytestmark = pytest.mark.aws_only

ORDERS = "/api/v1/orders"


@pytest.fixture(scope="module")
def public_address() -> str:
    return public_ipv4()


@pytest.fixture(scope="module")
def api_over_ipv4(endpoints: dict[str, Any]) -> Iterator[httpx.Client]:
    """The contract-checked client, over IPv4 like the echo service's answer."""
    with contract_checked_client(
        endpoints["apiUrl"], transport=ipv4_transport(), timeout=30
    ) as client:
        yield client


@pytest.fixture(scope="module", autouse=True)
def tiny_ip_limits(endpoints: dict[str, Any]) -> Iterator[RateLimits]:
    with preset_in_force(endpoints, "tiny-ip") as limits:
        yield limits


@pytest.fixture(autouse=True)
def _from_zero(endpoints: dict[str, Any], tiny_ip_limits: RateLimits) -> None:
    start_from_zero(endpoints)


def ip_keys(endpoints: dict[str, Any]) -> dict[str, int]:
    return counts(endpoints, RateLimitScope.IP)


def test_source_ip_is_the_clients_public_address_behind_the_edge(
    api_over_ipv4: httpx.Client,
    token_for: Callable[[str], str],
    endpoints: dict[str, Any],
    public_address: str,
) -> None:
    assert api_over_ipv4.get(ORDERS, headers=bearer(token_for("alice"))).status_code == 200

    [key] = ip_keys(endpoints)

    # Not one of CloudFront's addresses in front of the regional API (ADR 0006).
    assert key.startswith(f"ip#{ip_subject(public_address)}#")


def test_the_ip_limit_counts_the_clients_real_address(
    api_over_ipv4: httpx.Client,
    token_for: Callable[[str], str],
    endpoints: dict[str, Any],
    public_address: str,
    tiny_ip_limits: RateLimits,
) -> None:
    users = ("alice", "bob", "manager", "admin")
    statuses = [
        api_over_ipv4.get(ORDERS, headers=bearer(token_for(user))).status_code
        for user in users[: tiny_ip_limits.per_ip]
    ]

    refused = api_over_ipv4.get(ORDERS, headers=bearer(token_for(users[-1])))

    assert statuses == [200] * tiny_ip_limits.per_ip
    assert_problem(refused, 429, "rate-limited")
    assert refused.json()["scope"] == "ip"
    [(key, count)] = ip_keys(endpoints).items()
    assert key.startswith(f"ip#{ip_subject(public_address)}#")
    assert count == tiny_ip_limits.per_ip  # the refused request isn't counted
