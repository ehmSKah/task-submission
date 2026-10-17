"""The rate limiter: per IP first, then per user, one conditional count each (ADR 0006)."""

from datetime import UTC, datetime, timedelta

import pytest

from orders_api.application.rate_limiting import RateLimiter
from orders_api.domain.access import Role
from orders_api.domain.rate_limits import LimitUsage, RateLimits, RateLimitScope
from tests.unit.fakes import (
    FailingRateLimitCounters,
    FixedClock,
    InMemoryRateLimitCounters,
    SwitchableRateLimitConfig,
)

NOW = datetime(2026, 10, 17, 9, 31, 42, tzinfo=UTC)
WINDOW = "2026-10-17T09:31Z"
IP = "203.0.113.7"
USER = frozenset({Role.USER})
TINY = RateLimits(per_ip=5, per_role={Role.USER: 3, Role.ORDER_MANAGER: 4, Role.ADMIN: 4})


def limiter(
    limits: RateLimits = TINY,
    counters: InMemoryRateLimitCounters | None = None,
    now: datetime = NOW,
) -> tuple[RateLimiter, InMemoryRateLimitCounters]:
    counters = counters or InMemoryRateLimitCounters()
    return RateLimiter(counters, SwitchableRateLimitConfig(limits), FixedClock(now)), counters


def test_an_allowed_request_counts_once_per_ip_and_once_per_user() -> None:
    rate_limiter, counters = limiter()

    decision = rate_limiter.check(IP, "alice-sub", USER)

    assert decision.allowed
    assert counters.counts == {f"ip#{IP}#{WINDOW}": 1, f"user#alice-sub#{WINDOW}": 1}


def test_counters_expire_a_minute_after_their_window_ends() -> None:
    rate_limiter, counters = limiter()

    rate_limiter.check(IP, "alice-sub", USER)

    window_end = datetime(2026, 10, 17, 9, 32, tzinfo=UTC)
    assert set(counters.expiry.values()) == {window_end + timedelta(seconds=60)}


def test_the_user_limit_follows_the_caller_s_tier() -> None:
    rate_limiter, _ = limiter()

    decisions = [rate_limiter.check(IP, "alice-sub", USER) for _ in range(4)]

    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[-1].usage == LimitUsage(RateLimitScope.USER, limit=3, remaining=0)


def test_combined_roles_get_the_highest_tier() -> None:
    rate_limiter, _ = limiter()
    roles = frozenset({Role.USER, Role.ORDER_MANAGER})

    decisions = [rate_limiter.check(IP, "manager-sub", roles) for _ in range(5)]

    assert [d.allowed for d in decisions] == [True, True, True, True, False]


def test_a_caller_without_a_role_is_limited_too() -> None:
    rate_limiter, _ = limiter()

    decisions = [rate_limiter.check(IP, "nobody-sub", frozenset()) for _ in range(4)]

    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[-1].exceeded and decisions[-1].usage.scope is RateLimitScope.USER


def test_a_request_over_the_user_limit_doesn_t_increment_the_user_counter() -> None:
    rate_limiter, counters = limiter()

    for _ in range(6):
        rate_limiter.check(IP, "alice-sub", USER)

    assert counters.counts[f"user#alice-sub#{WINDOW}"] == 3


def test_the_ip_is_checked_first_and_a_request_over_it_touches_no_user_counter() -> None:
    rate_limiter, counters = limiter()
    subjects = ["alice-sub", "bob-sub", "carol-sub", "dave-sub", "erin-sub", "frank-sub"]

    decisions = [rate_limiter.check(IP, subject, USER) for subject in subjects]

    assert [d.allowed for d in decisions] == [True] * 5 + [False]
    assert decisions[-1].exceeded and decisions[-1].usage.scope is RateLimitScope.IP
    assert decisions[-1].usage == LimitUsage(RateLimitScope.IP, limit=5, remaining=0)
    assert counters.counts[f"ip#{IP}#{WINDOW}"] == 5
    assert f"user#frank-sub#{WINDOW}" not in counters.counts


def test_a_request_over_its_user_limit_still_counts_against_its_ip() -> None:
    """Rejected requests don't increment the counter that rejected them. The IP was counted
    before the user's limit was known to be reached."""
    rate_limiter, counters = limiter()

    for _ in range(4):
        rate_limiter.check(IP, "alice-sub", USER)

    assert counters.counts[f"ip#{IP}#{WINDOW}"] == 4


def test_ipv6_clients_are_counted_per_64() -> None:
    rate_limiter, counters = limiter()

    rate_limiter.check("2001:db8:1:2::1", "alice-sub", USER)
    rate_limiter.check("2001:db8:1:2::99", "bob-sub", USER)

    assert counters.counts[f"ip#2001:db8:1:2::/64#{WINDOW}"] == 2


def test_the_headers_describe_the_more_restrictive_limit() -> None:
    rate_limiter, _ = limiter(
        RateLimits(per_ip=4, per_role={Role.USER: 3, Role.ORDER_MANAGER: 9, Role.ADMIN: 9})
    )

    first = rate_limiter.check(IP, "alice-sub", USER)
    second = rate_limiter.check(IP, "manager-sub", frozenset({Role.ORDER_MANAGER}))

    assert first.usage == LimitUsage(RateLimitScope.USER, limit=3, remaining=2)
    assert second.usage == LimitUsage(RateLimitScope.IP, limit=4, remaining=2)


def test_the_reset_is_the_time_left_in_the_window() -> None:
    rate_limiter, _ = limiter()

    assert rate_limiter.check(IP, "alice-sub", USER).reset_seconds == 18


def test_a_new_window_starts_from_zero() -> None:
    counters = InMemoryRateLimitCounters()
    before, _ = limiter(counters=counters)
    for _ in range(4):
        before.check(IP, "alice-sub", USER)
    after, _ = limiter(counters=counters, now=NOW + timedelta(seconds=20))

    decision = after.check(IP, "alice-sub", USER)

    assert decision.allowed
    assert decision.usage.remaining == 2


def test_a_limit_lowered_below_the_count_rejects_with_nothing_left() -> None:
    config = SwitchableRateLimitConfig(
        RateLimits(per_ip=50, per_role={Role.USER: 10, Role.ORDER_MANAGER: 10, Role.ADMIN: 10})
    )
    rate_limiter = RateLimiter(InMemoryRateLimitCounters(), config, FixedClock(NOW))
    for _ in range(5):
        rate_limiter.check(IP, "alice-sub", USER)

    config.limits = RateLimits(
        per_ip=50, per_role={Role.USER: 3, Role.ORDER_MANAGER: 3, Role.ADMIN: 3}
    )
    decision = rate_limiter.check(IP, "alice-sub", USER)

    assert decision.exceeded and decision.usage.scope is RateLimitScope.USER
    assert decision.usage == LimitUsage(RateLimitScope.USER, limit=3, remaining=0)


def test_the_configuration_is_read_on_every_check() -> None:
    config = SwitchableRateLimitConfig(TINY)
    rate_limiter = RateLimiter(InMemoryRateLimitCounters(), config, FixedClock(NOW))
    rate_limiter.check(IP, "alice-sub", USER)

    config.limits = RateLimits(
        per_ip=100, per_role={Role.USER: 50, Role.ORDER_MANAGER: 50, Role.ADMIN: 50}
    )

    assert rate_limiter.check(IP, "alice-sub", USER).usage.limit == 50


def test_a_counter_store_failure_propagates() -> None:
    """Failing open belongs to a wrapper around the store, not to the limiter (ADR 0006)."""
    rate_limiter = RateLimiter(
        FailingRateLimitCounters(), SwitchableRateLimitConfig(TINY), FixedClock(NOW)
    )

    with pytest.raises(ConnectionError):
        rate_limiter.check(IP, "alice-sub", USER)
