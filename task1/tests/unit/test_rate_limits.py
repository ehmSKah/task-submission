"""Rate-limit windows, counter keys, role tiers and the limit the headers describe (ADR 0006)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from orders_api.domain.access import Role
from orders_api.domain.rate_limits import (
    DEFAULT_RATE_LIMITS,
    InvalidRateLimits,
    LimitUsage,
    RateLimits,
    RateLimitScope,
    counter_key,
    ip_subject,
    logged_network,
    more_restrictive,
    window_of,
)

TIERS = {Role.USER: 60, Role.ORDER_MANAGER: 300, Role.ADMIN: 300}


def test_a_window_is_the_minute_the_moment_falls_in() -> None:
    window = window_of(datetime(2026, 10, 17, 9, 31, 42, 500_000, tzinfo=UTC))

    assert window.start == datetime(2026, 10, 17, 9, 31, tzinfo=UTC)
    assert window.end == datetime(2026, 10, 17, 9, 32, tzinfo=UTC)


def test_a_window_starts_on_its_first_instant_and_the_next_one_on_its_end() -> None:
    start = datetime(2026, 10, 17, 9, 31, tzinfo=UTC)

    assert window_of(start).start == start
    assert window_of(start + timedelta(seconds=60)).start == start + timedelta(seconds=60)
    assert window_of(start - timedelta(microseconds=1)).start == start - timedelta(seconds=60)


def test_windows_are_counted_in_utc_whatever_the_clock_s_zone() -> None:
    local = datetime(2026, 10, 17, 11, 31, 5, tzinfo=timezone(timedelta(hours=2)))

    assert window_of(local).start == datetime(2026, 10, 17, 9, 31, tzinfo=UTC)


@pytest.mark.parametrize(
    ("second", "expected"), [(0.0, 60), (0.2, 60), (42.5, 18), (59.0, 1), (59.999, 1)]
)
def test_seconds_left_round_up_and_never_reach_zero(second: float, expected: int) -> None:
    now = datetime(2026, 10, 17, 9, 31, tzinfo=UTC) + timedelta(seconds=second)

    assert window_of(now).seconds_left(now) == expected


def test_the_key_names_scope_subject_and_window_start() -> None:
    window = window_of(datetime(2026, 10, 17, 9, 31, 42, tzinfo=UTC))

    assert (
        counter_key(RateLimitScope.IP, "203.0.113.7", window) == "ip#203.0.113.7#2026-10-17T09:31Z"
    )
    assert (
        counter_key(RateLimitScope.USER, "alice-sub", window) == "user#alice-sub#2026-10-17T09:31Z"
    )


def test_consecutive_windows_have_different_keys() -> None:
    now = datetime(2026, 10, 17, 9, 31, 59, tzinfo=UTC)

    assert counter_key(RateLimitScope.USER, "a", window_of(now)) != counter_key(
        RateLimitScope.USER, "a", window_of(now + timedelta(seconds=1))
    )


def test_an_ipv4_address_is_counted_on_its_own() -> None:
    assert ip_subject("203.0.113.7") == "203.0.113.7"


@pytest.mark.parametrize(
    "address",
    [
        "2001:db8:1:2:3:4:5:6",
        "2001:db8:1:2::1",
        "2001:0db8:0001:0002:ffff:ffff:ffff:ffff",
        "2001:DB8:1:2::abcd",
    ],
)
def test_every_ipv6_address_in_one_64_shares_a_subject(address: str) -> None:
    assert ip_subject(address) == "2001:db8:1:2::/64"


def test_neighbouring_ipv6_64s_are_counted_apart() -> None:
    assert ip_subject("2001:db8:1:2::1") != ip_subject("2001:db8:1:3::1")


def test_an_ipv4_mapped_ipv6_address_counts_as_its_ipv4_address() -> None:
    assert ip_subject("::ffff:203.0.113.7") == "203.0.113.7"


def test_a_source_that_is_no_ip_address_is_counted_as_given() -> None:
    assert ip_subject("test-invoke-source-ip") == "test-invoke-source-ip"


@pytest.mark.parametrize(
    ("address", "network"),
    [
        ("203.0.113.7", "203.0.113.0/24"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        ("::ffff:203.0.113.7", "203.0.113.0/24"),
        ("not-an-address", None),
    ],
)
def test_logs_see_only_the_network_never_the_address(address: str, network: str | None) -> None:
    """IP addresses are personal data: logs keep IPv4 `/24` and IPv6 `/64` (ADR 0012)."""
    assert logged_network(address) == network


def test_the_default_limits_are_the_adr_s() -> None:
    assert DEFAULT_RATE_LIMITS.per_ip == 300
    assert dict(DEFAULT_RATE_LIMITS.per_role) == TIERS


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        ({Role.USER}, 60),
        ({Role.ORDER_MANAGER}, 300),
        ({Role.ADMIN}, 300),
        ({Role.USER, Role.ORDER_MANAGER}, 300),
        ({Role.USER, Role.ADMIN}, 300),
    ],
)
def test_the_highest_tier_among_the_caller_s_roles_wins(roles: set[Role], expected: int) -> None:
    assert DEFAULT_RATE_LIMITS.per_user(frozenset(roles)) == expected


def test_tiers_combine_whichever_role_holds_the_highest() -> None:
    limits = RateLimits(
        per_ip=300, per_role={Role.USER: 90, Role.ORDER_MANAGER: 30, Role.ADMIN: 10}
    )

    assert limits.per_user(frozenset({Role.USER, Role.ADMIN})) == 90
    assert limits.per_user(frozenset({Role.ORDER_MANAGER, Role.ADMIN})) == 30


def test_a_caller_without_a_role_gets_the_lowest_tier() -> None:
    limits = RateLimits(
        per_ip=300, per_role={Role.USER: 60, Role.ORDER_MANAGER: 20, Role.ADMIN: 90}
    )

    assert DEFAULT_RATE_LIMITS.per_user(frozenset()) == 60
    assert limits.per_user(frozenset()) == 20


def test_every_role_needs_a_tier() -> None:
    with pytest.raises(InvalidRateLimits, match="ADMIN"):
        RateLimits(per_ip=300, per_role={Role.USER: 60, Role.ORDER_MANAGER: 300})


@pytest.mark.parametrize(
    "limits",
    [
        {"per_ip": 0, "per_role": TIERS},
        {"per_ip": 300, "per_role": {**TIERS, Role.USER: 0}},
        {"per_ip": -1, "per_role": TIERS},
    ],
)
def test_every_limit_lets_at_least_one_request_through(limits: dict[str, object]) -> None:
    with pytest.raises(InvalidRateLimits):
        RateLimits(**limits)  # type: ignore[arg-type]


def test_the_limits_keep_no_reference_to_the_mapping_they_were_given() -> None:
    tiers = dict(TIERS)
    limits = RateLimits(per_ip=300, per_role=tiers)
    tiers[Role.USER] = 1

    assert limits.per_user(frozenset({Role.USER})) == 60


def ip(limit: int, remaining: int) -> LimitUsage:
    return LimitUsage(RateLimitScope.IP, limit=limit, remaining=remaining)


def user(limit: int, remaining: int) -> LimitUsage:
    return LimitUsage(RateLimitScope.USER, limit=limit, remaining=remaining)


@pytest.mark.parametrize(
    ("usages", "expected"),
    [
        ((ip(300, 299), user(60, 59)), user(60, 59)),
        ((ip(300, 10), user(60, 59)), ip(300, 10)),
        ((ip(300, 0), user(60, 5)), ip(300, 0)),
        # Equally many left: the smaller limit is the stricter policy.
        ((ip(300, 5), user(60, 5)), user(60, 5)),
        ((ip(60, 5), user(300, 5)), ip(60, 5)),
        # Identical: the first one, so the choice is stable.
        ((ip(60, 5), user(60, 5)), ip(60, 5)),
    ],
)
def test_the_headers_describe_the_limit_with_the_fewest_requests_left(
    usages: tuple[LimitUsage, LimitUsage], expected: LimitUsage
) -> None:
    assert more_restrictive(*usages) == expected
