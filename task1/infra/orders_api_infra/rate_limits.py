"""Switch the runtime rate limits, and the rate-limit helpers for tests (ADRs 0006 and 0011).

`make limits PRESET=…` writes a preset into the environment's SSM parameter. Every Lambda
instance picks it up within the environment's cache time, without a deploy. The test helpers
(switching and waiting, emptying the counters) run with developer or CI credentials and only
where the environment sets `testFixtures`.

Usage: python -m orders_api_infra.rate_limits --env local --preset tiny
"""

import argparse
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import boto3

from orders_api.adapters.outbound.dynamodb_rate_limits import KEY_ATTRIBUTE
from orders_api.adapters.outbound.ssm_rate_limits import rate_limits_to_json
from orders_api.domain.access import Role
from orders_api.domain.rate_limits import DEFAULT_RATE_LIMITS, RateLimits, window_of
from orders_api_infra.config import EnvConfig, load_config
from orders_api_infra.endpoints import stack_outputs

PRESETS: dict[str, RateLimits] = {
    # ADR 0006's limits, which CDK creates the parameter with.
    "defaults": DEFAULT_RATE_LIMITS,
    # For every suite but the rate-limit tests: they send far more than 60 requests a minute per
    # demo user, all from one address locally (ADR 0011).
    "high": RateLimits(per_ip=100_000, per_role=dict.fromkeys(Role, 100_000)),
    # A USER is refused after 3 requests, staff after 5; the IP limit stays above them all.
    "tiny": RateLimits(per_ip=30, per_role={Role.USER: 3, Role.ORDER_MANAGER: 5, Role.ADMIN: 5}),
    # The IP is refused after 3 requests, before any user is: locally every request comes from
    # the same Docker address.
    "tiny-ip": RateLimits(per_ip=3, per_role=dict.fromkeys(Role, 10)),
}

CACHE_MARGIN_SECONDS = 1.0
"""Added to the cache time when waiting, for requests already under way."""
WINDOW_MARGIN_SECONDS = 0.1
"""Added when waiting for the next window, so the wait ends inside it."""


def read_limits(config: EnvConfig) -> str:
    """The parameter's value as stored, valid or not."""
    parameter = boto3.client("ssm").get_parameter(Name=config.rate_limits_parameter)
    return parameter["Parameter"]["Value"]


def write_limits(config: EnvConfig, value: str) -> None:
    """Store `value` as the environment's rate limits; the function validates it on reading."""
    boto3.client("ssm").put_parameter(
        Name=config.rate_limits_parameter, Value=value, Type="String", Overwrite=True
    )


def _require_test_fixtures(config: EnvConfig) -> None:
    if not config.test_fixtures:
        raise SystemExit(
            f"Refusing: environment '{config.name}' has testFixtures: false (ADR 0011)"
        )


def wait_for_cache(config: EnvConfig, sleep: Callable[[float], None] = time.sleep) -> None:
    """Until every Lambda instance has read the parameter again."""
    _require_test_fixtures(config)
    sleep(config.rate_limit_config_cache_seconds + CACHE_MARGIN_SECONDS)


@contextmanager
def switched_limits(config: EnvConfig, value: str, *, restore: str | None = None) -> Iterator[None]:
    """`value` in force inside the block, and afterwards `restore` (by default the previous
    value), also when the block fails. Both switches wait out the cache."""
    _require_test_fixtures(config)
    afterwards = read_limits(config) if restore is None else restore
    write_limits(config, value)
    wait_for_cache(config)
    try:
        yield
    finally:
        write_limits(config, afterwards)
        wait_for_cache(config)


@contextmanager
def preset_in_force(
    config: EnvConfig, name: str, *, afterwards: str | None = None
) -> Iterator[RateLimits]:
    """`switched_limits` by preset name; `afterwards` names the preset to restore instead of the
    previous value."""
    restore = None if afterwards is None else rate_limits_to_json(PRESETS[afterwards])
    with switched_limits(config, rate_limits_to_json(PRESETS[name]), restore=restore):
        yield PRESETS[name]


def empty_counters(config: EnvConfig) -> int:
    """Delete every rate-limit counter, so a test starts from zero; returns how many there were."""
    _require_test_fixtures(config)
    table = boto3.resource("dynamodb").Table(stack_outputs(config)["RateLimitTableName"])
    deleted = 0
    scan_kwargs: dict[str, Any] = {
        "ProjectionExpression": "#key",
        "ExpressionAttributeNames": {"#key": KEY_ATTRIBUTE},
    }
    with table.batch_writer() as batch:
        while True:
            page = table.scan(**scan_kwargs)
            for item in page["Items"]:
                batch.delete_item(Key={KEY_ATTRIBUTE: item[KEY_ATTRIBUTE]})
                deleted += 1
            if "LastEvaluatedKey" not in page:
                return deleted
            scan_kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def wait_for_window(
    seconds_needed: float,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Return once at least `seconds_needed` remain in the current window, waiting for the next
    one if necessary, so a test's requests all fall into one window."""
    moment = now()
    left = (window_of(moment).end - moment).total_seconds()
    if left < seconds_needed:
        sleep(left + WINDOW_MARGIN_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="local")
    parser.add_argument("--preset", choices=list(PRESETS), required=True)
    args = parser.parse_args()
    config = load_config(args.env)
    value = rate_limits_to_json(PRESETS[args.preset])
    write_limits(config, value)
    print(
        f"{config.rate_limits_parameter} = {value}\n"
        f"In force within {config.rate_limit_config_cache_seconds} s."
    )


if __name__ == "__main__":
    main()
