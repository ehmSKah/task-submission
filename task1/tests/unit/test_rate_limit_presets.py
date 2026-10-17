"""`make limits` presets and the rate-limit test helpers (ADRs 0006 and 0011)."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from orders_api.domain.access import Role
from orders_api.domain.rate_limits import DEFAULT_RATE_LIMITS
from orders_api_infra import rate_limits
from orders_api_infra.rate_limits import PRESETS, switched_limits, wait_for_window
from tests.unit.data_stack import config

ALL_ROLES = frozenset(Role)


def test_the_defaults_preset_is_what_cdk_creates() -> None:
    assert PRESETS["defaults"] == DEFAULT_RATE_LIMITS


def test_high_limits_leave_room_for_every_test_suite() -> None:
    high = PRESETS["high"]

    assert high.per_ip >= 10_000
    assert min(high.per_role.values()) >= 10_000


def test_tiny_limits_refuse_a_user_before_the_ip() -> None:
    tiny = PRESETS["tiny"]

    assert tiny.per_user(frozenset({Role.USER})) <= 5
    assert tiny.per_ip > sum(tiny.per_role.values())


def test_tiny_ip_limits_refuse_the_ip_before_any_user() -> None:
    """Locally every request comes from one address (ADR 0011)."""
    tiny_ip = PRESETS["tiny-ip"]

    assert tiny_ip.per_ip < tiny_ip.per_user(frozenset())


@pytest.mark.parametrize(("second", "slept"), [(0.0, None), (49.0, None), (50.5, 9.6), (59.9, 0.2)])
def test_a_test_starts_with_enough_of_its_window_left(second: float, slept: float | None) -> None:
    moment = datetime(2026, 10, 17, 9, 31, tzinfo=UTC).timestamp() + second
    sleeps: list[float] = []

    wait_for_window(10, now=lambda: datetime.fromtimestamp(moment, UTC), sleep=sleeps.append)

    assert sleeps == ([] if slept is None else [pytest.approx(slept)])


def test_the_test_helpers_refuse_to_run_without_test_fixtures() -> None:
    production = config(testFixtures=False)

    with pytest.raises(SystemExit, match="testFixtures"):
        rate_limits.empty_counters(production)
    with pytest.raises(SystemExit, match="testFixtures"), switched_limits(production, "{}"):
        pass


@pytest.fixture
def parameter(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """The parameter's values over time, without AWS and without waiting."""
    values = ["previous"]
    monkeypatch.setattr(rate_limits, "read_limits", lambda _: values[-1])
    monkeypatch.setattr(rate_limits, "write_limits", lambda _, value: values.append(value))
    monkeypatch.setattr(rate_limits, "wait_for_cache", lambda _: None)
    yield values


def test_switched_limits_restore_the_previous_value_even_when_the_test_fails(
    parameter: list[str],
) -> None:
    with pytest.raises(AssertionError), switched_limits(config(), "tiny"):
        assert parameter[-1] == "tiny"
        raise AssertionError("the test failed")

    assert parameter == ["previous", "tiny", "previous"]


def test_switched_limits_can_restore_a_given_value_instead_of_the_previous_one(
    parameter: list[str],
) -> None:
    with switched_limits(config(), "high", restore="defaults"):
        pass

    assert parameter == ["previous", "high", "defaults"]
