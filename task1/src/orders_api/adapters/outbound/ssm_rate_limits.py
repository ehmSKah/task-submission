"""The rate limits in force, from one JSON parameter in SSM Parameter Store (ADRs 0006 and 0009).

Operators change the parameter at runtime; every Lambda instance reads it through Powertools
Parameters, cached for a configured number of seconds. A value that breaks the schema is logged
and ignored: the last valid one (or the defaults) stays in force.
"""

import json
from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.parameters import SSMProvider
from aws_lambda_powertools.utilities.parameters.base import BaseProvider
from aws_lambda_powertools.utilities.validation import SchemaValidationError, validate

from orders_api.domain.access import Role
from orders_api.domain.rate_limits import DEFAULT_RATE_LIMITS, InvalidRateLimits, RateLimits

MAX_LIMIT = 1_000_000
"""Requests per minute; anything above is a typo, not a limit."""

_LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}
RATE_LIMITS_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-04/schema#",
    "type": "object",
    "additionalProperties": False,
    "required": ["perIp", "perUser"],
    "properties": {
        "perIp": _LIMIT,
        "perUser": {
            "description": "Per role; a caller with several roles gets the highest (ADR 0006).",
            "type": "object",
            "additionalProperties": False,
            "required": [role.value for role in Role],
            "properties": {role.value: _LIMIT for role in Role},
        },
    },
}

logger = Logger(service="orders-api", child=True)


def _reject_constant(constant: str) -> float:
    raise ValueError(f"{constant} is not JSON")


def rate_limits_from_json(text: str) -> RateLimits:
    """The limits a stored value describes; `InvalidRateLimits` unless it matches the schema."""
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except ValueError as error:
        raise InvalidRateLimits(f"Not JSON: {error}") from None
    try:
        validate(event=value, schema=RATE_LIMITS_SCHEMA)
    except SchemaValidationError as error:
        raise InvalidRateLimits(str(error.validation_message or error)) from None
    return RateLimits(
        per_ip=int(value["perIp"]),
        per_role={Role(name): int(limit) for name, limit in value["perUser"].items()},
    )


def rate_limits_to_json(limits: RateLimits) -> str:
    """The stored form: `{"perIp": 300, "perUser": {"USER": 60, …}}`."""
    per_user = {role.value: limits.per_role[role] for role in Role}
    return json.dumps({"perIp": limits.per_ip, "perUser": per_user})


class SsmRateLimitConfig:
    """A `RateLimitConfigSource` reading one SSM parameter, with a last-known-good fallback."""

    def __init__(
        self,
        parameter_name: str,
        *,
        max_age_seconds: int,
        provider: BaseProvider | None = None,
        fallback: RateLimits = DEFAULT_RATE_LIMITS,
    ) -> None:
        self._name = parameter_name
        self._max_age = max_age_seconds
        self._provider = provider or SSMProvider()
        self._last_good = fallback
        self._last_value: str | None = None

    def current(self) -> RateLimits:
        """The parameter's limits, re-read once the cache expires. The raw value is cached, not
        the parsed one, so an invalid value isn't fetched again on every request either."""
        value = str(self._provider.get(self._name, max_age=self._max_age))
        if value != self._last_value:
            self._last_value = value
            try:
                self._last_good = rate_limits_from_json(value)
            except InvalidRateLimits as error:
                # Once per value: the reason, never the value itself.
                logger.error(
                    "rate_limit_config_invalid",
                    extra={"parameter": self._name, "reason": str(error)},
                )
        return self._last_good
