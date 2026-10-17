"""The rate-limit configuration as stored, and the last known good one (ADRs 0006 and 0009)."""

import io
import json
import time
from typing import Any

import pytest
from aws_lambda_powertools.utilities.parameters.base import BaseProvider

from orders_api.adapters.outbound.ssm_rate_limits import (
    SsmRateLimitConfig,
    rate_limits_from_json,
    rate_limits_to_json,
)
from orders_api.domain.access import Role
from orders_api.domain.rate_limits import DEFAULT_RATE_LIMITS, InvalidRateLimits, RateLimits

PARAMETER = "/orders-api/test/rate-limits"
TINY = RateLimits(per_ip=5, per_role={Role.USER: 3, Role.ORDER_MANAGER: 4, Role.ADMIN: 4})
VALID: dict[str, Any] = {"perIp": 5, "perUser": {"USER": 3, "ORDER_MANAGER": 4, "ADMIN": 4}}


class InMemoryParameters(BaseProvider):
    """Parameter Store in memory, behind Powertools' own provider and its cache."""

    def __init__(self, values: dict[str, str]) -> None:
        super().__init__()
        self.values = values
        self.reads = 0

    def _get(self, name: str, **sdk_options: Any) -> str:
        self.reads += 1
        return self.values[name]

    def _get_multiple(self, path: str, **sdk_options: Any) -> dict[str, str]:
        raise NotImplementedError


def source(value: str, max_age: int = 60) -> tuple[SsmRateLimitConfig, InMemoryParameters]:
    parameters = InMemoryParameters({PARAMETER: value})
    return SsmRateLimitConfig(PARAMETER, max_age_seconds=max_age, provider=parameters), parameters


def test_the_stored_format_round_trips() -> None:
    assert rate_limits_from_json(rate_limits_to_json(DEFAULT_RATE_LIMITS)) == DEFAULT_RATE_LIMITS
    assert json.loads(rate_limits_to_json(TINY)) == VALID


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        "[]",
        json.dumps({"perIp": 5}),
        json.dumps({**VALID, "perRoute": 3}),
        json.dumps({**VALID, "perUser": {"USER": 3, "ORDER_MANAGER": 4}}),
        json.dumps({**VALID, "perUser": {**VALID["perUser"], "AUDITOR": 4}}),
        json.dumps({**VALID, "perIp": "300"}),
        json.dumps({**VALID, "perIp": 2.5}),
        json.dumps({**VALID, "perIp": True}),
        json.dumps({**VALID, "perIp": 0}),
        json.dumps({**VALID, "perIp": 1_000_001}),
        json.dumps({**VALID, "perUser": {**VALID["perUser"], "USER": -1}}),
        "NaN",
    ],
)
def test_a_value_that_breaks_the_schema_is_rejected(value: str) -> None:
    with pytest.raises(InvalidRateLimits):
        rate_limits_from_json(value)


def test_the_current_limits_are_read_from_the_parameter() -> None:
    config, _ = source(json.dumps(VALID))

    assert config.current() == TINY


def test_reads_are_cached_for_the_configured_time() -> None:
    config, parameters = source(json.dumps(VALID), max_age=60)

    config.current()
    parameters.values[PARAMETER] = rate_limits_to_json(DEFAULT_RATE_LIMITS)

    assert config.current() == TINY
    assert parameters.reads == 1


def test_a_changed_value_takes_effect_once_the_cache_expires() -> None:
    config, parameters = source(json.dumps(VALID), max_age=1)
    config.current()

    parameters.values[PARAMETER] = rate_limits_to_json(DEFAULT_RATE_LIMITS)
    time.sleep(1.1)

    assert config.current() == DEFAULT_RATE_LIMITS


def invalid_config_lines(log_output: io.StringIO) -> list[dict[str, Any]]:
    lines = [json.loads(line) for line in log_output.getvalue().splitlines() if line]
    return [line for line in lines if line["message"] == "rate_limit_config_invalid"]


def test_an_invalid_value_leaves_the_last_known_good_limits_in_force(
    log_output: io.StringIO,
) -> None:
    config, parameters = source(json.dumps(VALID), max_age=0)
    config.current()

    parameters.values[PARAMETER] = json.dumps({**VALID, "perIp": 0})

    assert config.current() == TINY
    [line] = invalid_config_lines(log_output)
    assert line["level"] == "ERROR"
    assert line["parameter"] == PARAMETER
    assert "perIp" in line["reason"]


def test_without_a_valid_value_yet_the_defaults_are_in_force(log_output: io.StringIO) -> None:
    config, _ = source("{", max_age=0)

    assert config.current() == DEFAULT_RATE_LIMITS
    assert len(invalid_config_lines(log_output)) == 1


def test_an_invalid_value_is_logged_once_not_on_every_read(log_output: io.StringIO) -> None:
    config, parameters = source("{", max_age=0)

    for _ in range(3):
        config.current()

    assert parameters.reads == 3
    assert len(invalid_config_lines(log_output)) == 1


def test_a_valid_value_after_an_invalid_one_takes_effect() -> None:
    config, parameters = source("{", max_age=0)
    config.current()

    parameters.values[PARAMETER] = json.dumps(VALID)

    assert config.current() == TINY
