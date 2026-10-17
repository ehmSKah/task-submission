"""WAF really blocks: an address over the limit gets the custom 429 at the edge (ADRs 0006, 0011
and 0012). AWS only: Floci deploys the web ACL but filters nothing.

The test trips aws-dev's test-only rule, which counts only requests carrying the test header, so
the other suites' requests from the same address never count and are never blocked. That rule
answers like the flood rule. WAF counts over a 5-minute window and acts after a delay of its own,
so requests are sent at a steady pace until the first `429`, within a fixed time and number. The
block then lifts by itself within the window; nothing needs restoring.
"""

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
import httpx
import pytest
from openapi_schema_validator import OAS30Validator

from orders_api.adapters.inbound.cors import static_cors_headers
from orders_api.adapters.inbound.http_headers import SECURITY_HEADERS
from orders_api_infra.config import load_config
from orders_api_infra.openapi_extensions import load_spec
from orders_api_infra.waf import (
    RETRY_AFTER_SECONDS,
    TEST_HEADER,
    TEST_HEADER_VALUE,
    TEST_RULE,
    rate_limited_problem,
)
from tests.integration.public_address import ipv4_transport, public_ipv4

pytestmark = pytest.mark.aws_only

HEALTH = "/api/v1/health"  # no token, no app limit: any 429 on it comes from in front of the API
PARALLEL = 5
REQUESTS_PER_SECOND = 20
SEND_FOR_AT_MOST_SECONDS = 240
KEYS_WITHIN_SECONDS = 60


@pytest.fixture(scope="module")
def edge(endpoints: dict[str, Any]) -> Iterator[httpx.Client]:
    """A plain client: WAF's `429` is `application/json`, the only JSON type WAF can send, where
    the contract documents `application/problem+json`, so its body is checked here instead."""
    if load_config(endpoints["env"]).waf_test_rule_limit is None:
        pytest.skip(f"environment '{endpoints['env']}' has no WAF test rule")
    with httpx.Client(
        base_url=endpoints["apiUrl"], transport=ipv4_transport(), timeout=10
    ) as client:
        yield client


def first_blocked(edge: httpx.Client) -> httpx.Response:
    """Send test-header requests at a steady pace until WAF answers `429`."""
    counted = {TEST_HEADER: TEST_HEADER_VALUE}
    deadline = time.monotonic() + SEND_FOR_AT_MOST_SECONDS
    with ThreadPoolExecutor(PARALLEL) as pool:
        while time.monotonic() < deadline:
            started = time.monotonic()
            responses = list(pool.map(lambda _: edge.get(HEALTH, headers=counted), range(PARALLEL)))
            for response in responses:
                if response.status_code == 429:
                    return response
                assert response.status_code == 200, response.text
            time.sleep(max(0.0, PARALLEL / REQUESTS_PER_SECOND - (time.monotonic() - started)))
    pytest.fail(f"WAF blocked nothing within {SEND_FOR_AT_MOST_SECONDS} s")


def managed_addresses(endpoints: dict[str, Any]) -> list[str]:
    """The addresses WAF is rate limiting under the test rule, waiting for them to show."""
    *_, name, acl_id = endpoints["webAclArn"].split("/")
    waf = boto3.client("wafv2")
    deadline = time.monotonic() + KEYS_WITHIN_SECONDS
    while True:
        keys = waf.get_rate_based_statement_managed_keys(
            Scope="REGIONAL", WebACLName=name, WebACLId=acl_id, RuleName=TEST_RULE
        )
        addresses: list[str] = keys.get("ManagedKeysIPV4", {}).get("Addresses", [])
        if addresses or time.monotonic() > deadline:
            return addresses
        time.sleep(5)


def test_waf_blocks_an_address_over_the_limit_with_the_rate_limited_problem(
    edge: httpx.Client, endpoints: dict[str, Any]
) -> None:
    public_address = public_ipv4()

    blocked = first_blocked(edge)

    assert blocked.headers["Content-Type"].startswith("application/json")
    assert blocked.json() == rate_limited_problem()
    problem_schema = {
        "$ref": "#/components/schemas/Problem",
        "components": load_spec()["components"],
    }
    OAS30Validator(problem_schema).validate(blocked.json())
    # WAF sets `Retry-After`; API Gateway adds its `DEFAULT_4XX` headers, the security headers and,
    # so a page from the one allowed origin can read the 429 too, the CORS ones (ADR 0014). Each
    # must arrive exactly once: every header that doesn't is reported, not only the first.
    origins = load_config(endpoints["env"]).cors_allowed_origins
    expected = {
        "Retry-After": str(RETRY_AFTER_SECONDS),
        **SECURITY_HEADERS,
        **static_cors_headers(origins),
    }
    arrived = {name: blocked.headers.get_list(name) for name in expected}
    assert {name: values for name, values in arrived.items() if values != [expected[name]]} == {}
    assert "RateLimit-Limit" not in blocked.headers
    # Only the test rule's requests are blocked: the same address is still served without the
    # header, so no other suite is ever affected.
    assert edge.get(HEALTH).status_code == 200
    # WAF's `IP` key is the client's address, not one of the edge's (ADR 0006).
    assert f"{public_address}/32" in managed_addresses(endpoints)
