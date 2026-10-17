"""The rate-limit configuration read from SSM Parameter Store through Powertools Parameters
(ADRs 0006 and 0009).

Each test writes its own throwaway parameter, so the deployed one is never disturbed; the last
test only reads the deployed one.
"""

import time
import uuid
from collections.abc import Iterator
from typing import Any

import boto3
import pytest

from orders_api.adapters.outbound.ssm_rate_limits import (
    SsmRateLimitConfig,
    rate_limits_from_json,
    rate_limits_to_json,
)
from orders_api.domain.access import Role
from orders_api.domain.rate_limits import DEFAULT_RATE_LIMITS, RateLimits

TINY = RateLimits(per_ip=5, per_role={Role.USER: 3, Role.ORDER_MANAGER: 4, Role.ADMIN: 4})


@pytest.fixture
def parameter(endpoints: dict[str, Any]) -> Iterator[str]:
    name = f"/orders-api/{endpoints['env']}/adapter-test/{uuid.uuid4()}"
    yield name
    boto3.client("ssm").delete_parameter(Name=name)


def put(name: str, value: str) -> None:
    boto3.client("ssm").put_parameter(Name=name, Value=value, Type="String", Overwrite=True)


def test_the_limits_are_read_from_the_parameter(parameter: str) -> None:
    put(parameter, rate_limits_to_json(TINY))

    assert SsmRateLimitConfig(parameter, max_age_seconds=2).current() == TINY


def test_a_change_takes_effect_once_the_cache_expires_and_not_before(parameter: str) -> None:
    put(parameter, rate_limits_to_json(TINY))
    config = SsmRateLimitConfig(parameter, max_age_seconds=2)
    config.current()

    put(parameter, rate_limits_to_json(DEFAULT_RATE_LIMITS))
    cached = config.current()
    time.sleep(2.2)

    assert cached == TINY
    assert config.current() == DEFAULT_RATE_LIMITS


def test_an_invalid_value_leaves_the_last_known_good_limits_in_force(parameter: str) -> None:
    put(parameter, rate_limits_to_json(TINY))
    config = SsmRateLimitConfig(parameter, max_age_seconds=1)
    config.current()

    put(parameter, '{"perIp": "lots"}')
    time.sleep(1.2)

    assert config.current() == TINY


def test_the_deployed_parameter_exists_and_holds_valid_limits(endpoints: dict[str, Any]) -> None:
    """CDK created it with the defaults; tests may since have switched in another preset."""
    stored = boto3.client("ssm").get_parameter(Name=endpoints["rateLimitsParameter"])["Parameter"]

    assert stored["Type"] == "String"
    rate_limits_from_json(stored["Value"])
