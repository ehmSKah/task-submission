"""Failing open when the counter store misbehaves: throttling is over the limit, an outage falls
back to counting in memory, and a circuit breaker spares requests the wait (ADR 0006)."""

from datetime import UTC, datetime, timedelta

import pytest

from orders_api.application.fail_open_counters import (
    FAILURES_TO_OPEN,
    FALLBACK_MAX_KEYS,
    OPEN_SECONDS,
    REST_AFTER_FAILURE_SECONDS,
    FailOpenCounterStore,
    LocalCounters,
)
from orders_api.application.rate_limiting import (
    CounterStoreThrottled,
    CounterStoreUnavailable,
    RateLimiter,
)
from orders_api.domain.access import Role
from orders_api.domain.rate_limits import LimitUsage, RateLimits, RateLimitScope
from tests.unit.fakes import (
    FailingRateLimitCounters,
    FixedClock,
    ManualTime,
    RecordedEvents,
    SwitchableRateLimitConfig,
    UnreliableRateLimitCounters,
)

NOW = datetime(2026, 10, 17, 9, 31, 42, tzinfo=UTC)
WINDOW = "2026-10-17T09:31Z"
EXPIRES_AT = datetime(2026, 10, 17, 9, 33, tzinfo=UTC)
IP = "203.0.113.7"
USER = frozenset({Role.USER})
TINY = RateLimits(per_ip=5, per_role={Role.USER: 3, Role.ORDER_MANAGER: 4, Role.ADMIN: 4})
TIMEOUT = CounterStoreUnavailable("ReadTimeoutError")
THROTTLED = CounterStoreThrottled("ProvisionedThroughputExceededException")


class Setup:
    """A fail-open store around unreliable counters, read through a real `RateLimiter`."""

    def __init__(self, fallback: LocalCounters | None = None) -> None:
        self.counters = UnreliableRateLimitCounters()
        self.time = ManualTime()
        self.events = RecordedEvents()
        self.store = FailOpenCounterStore(
            self.counters, events=self.events, time_source=self.time, fallback=fallback
        )
        self.limiter = RateLimiter(self.store, SwitchableRateLimitConfig(TINY), FixedClock(NOW))

    def count(self, key: str = "user#alice-sub#w", limit: int = 100) -> int | None:
        return self.store.increment(key, limit=limit, expires_at=EXPIRES_AT)

    def fail_until_open(self) -> None:
        """Consecutive failed calls, each after the rest the one before it earned, until the
        circuit opens."""
        self.counters.failure = TIMEOUT
        for _ in range(FAILURES_TO_OPEN):
            self.count()
            self.time.advance(REST_AFTER_FAILURE_SECONDS)


@pytest.fixture
def setup() -> Setup:
    return Setup()


# Throttling: the store answered, and the answer is "too much".


def test_a_throttled_counter_is_over_the_limit(setup: Setup) -> None:
    setup.counters.failure = THROTTLED

    decision = setup.limiter.check(IP, "alice-sub", USER)

    assert decision.exceeded
    assert decision.usage == LimitUsage(RateLimitScope.IP, limit=5, remaining=0)


def test_throttling_is_no_failure_the_store_keeps_being_asked(setup: Setup) -> None:
    setup.counters.failure = THROTTLED

    results = [setup.count() for _ in range(FAILURES_TO_OPEN + 2)]

    assert results == [None] * (FAILURES_TO_OPEN + 2)
    assert setup.counters.calls == FAILURES_TO_OPEN + 2
    assert setup.events.reported == []


# An outage: timeouts, network and service errors let the request through, counted in memory.


def test_an_unavailable_store_lets_the_request_through_counted_in_memory(setup: Setup) -> None:
    setup.counters.failure = TIMEOUT

    decision = setup.limiter.check(IP, "alice-sub", USER)

    assert decision.allowed
    assert decision.usage == LimitUsage(RateLimitScope.USER, limit=3, remaining=2)
    assert setup.counters.counts == {}


def test_the_fallback_enforces_the_same_limits_in_this_instance(setup: Setup) -> None:
    setup.counters.failure = TIMEOUT

    decisions = [setup.limiter.check(IP, "alice-sub", USER) for _ in range(4)]

    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[-1].usage == LimitUsage(RateLimitScope.USER, limit=3, remaining=0)


def test_the_fallback_checks_the_ip_first_too(setup: Setup) -> None:
    setup.counters.failure = TIMEOUT
    subjects = ["alice-sub", "bob-sub", "carol-sub", "dave-sub", "erin-sub", "frank-sub"]

    decisions = [setup.limiter.check(IP, subject, USER) for subject in subjects]

    assert [d.allowed for d in decisions] == [True] * 5 + [False]
    assert decisions[-1].usage.scope is RateLimitScope.IP


def test_a_failure_is_reported_as_rate_limiter_unavailable_with_its_cause(setup: Setup) -> None:
    setup.counters.failure = TIMEOUT

    setup.limiter.check(IP, "alice-sub", USER)

    assert setup.events.reported == [
        ("rate_limiter_unavailable", {"errorType": "ReadTimeoutError", "consecutiveFailures": 1})
    ]


def test_no_event_carries_the_counter_key(setup: Setup) -> None:
    """Counter keys hold the client's full address, which logs never do (ADR 0012)."""
    setup.fail_until_open()
    setup.time.advance(OPEN_SECONDS)
    setup.counters.failure = None
    setup.limiter.check(IP, "alice-sub", USER)

    assert setup.events.reported
    assert IP not in repr(setup.events.reported)
    assert "alice-sub" not in repr(setup.events.reported)


def test_errors_the_port_doesn_t_name_propagate() -> None:
    """A defect, e.g. a missing permission, is no outage to hide: it surfaces as a `500`."""
    store = FailOpenCounterStore(FailingRateLimitCounters(), time_source=ManualTime())

    with pytest.raises(ConnectionError):
        store.increment("ip#x#w", limit=5, expires_at=EXPIRES_AT)


# The time budget: a request pays for one failed call at most.


def test_a_request_pays_for_at_most_one_failed_call(setup: Setup) -> None:
    setup.counters.failure = TIMEOUT

    setup.limiter.check(IP, "alice-sub", USER)

    assert setup.counters.calls == 1


def test_after_a_failure_the_store_rests_briefly_then_is_asked_again(setup: Setup) -> None:
    setup.counters.failure = TIMEOUT
    setup.count()
    setup.counters.failure = None

    setup.time.advance(REST_AFTER_FAILURE_SECONDS - 0.01)
    setup.count()
    assert setup.counters.calls == 1

    setup.time.advance(0.01)
    assert setup.count() == 1
    assert setup.counters.calls == 2


# The circuit breaker.


def test_consecutive_failures_open_the_circuit(setup: Setup) -> None:
    setup.fail_until_open()

    assert setup.events.names == ["rate_limiter_unavailable"] * FAILURES_TO_OPEN + [
        "rate_limiter_circuit_open"
    ]
    assert setup.events.reported[-1] == (
        "rate_limiter_circuit_open",
        {
            "errorType": "ReadTimeoutError",
            "consecutiveFailures": FAILURES_TO_OPEN,
            "openSeconds": OPEN_SECONDS,
        },
    )


def test_one_failure_short_of_the_threshold_leaves_the_circuit_closed(setup: Setup) -> None:
    setup.counters.failure = TIMEOUT
    for _ in range(FAILURES_TO_OPEN - 1):
        setup.count()
        setup.time.advance(REST_AFTER_FAILURE_SECONDS)

    assert "rate_limiter_circuit_open" not in setup.events.names


def test_an_open_circuit_skips_the_store_and_logs_nothing_more(setup: Setup) -> None:
    setup.fail_until_open()
    calls, reported = setup.counters.calls, len(setup.events.reported)

    for _ in range(50):
        setup.limiter.check(IP, "alice-sub", USER)
    setup.time.advance(OPEN_SECONDS - REST_AFTER_FAILURE_SECONDS - 0.01)
    setup.count()

    assert setup.counters.calls == calls
    assert len(setup.events.reported) == reported


def test_an_open_circuit_still_enforces_the_limits_in_memory(setup: Setup) -> None:
    setup.fail_until_open()

    decisions = [setup.limiter.check(IP, "alice-sub", USER) for _ in range(4)]

    assert [d.allowed for d in decisions] == [True, True, True, False]


def test_an_answer_between_failures_starts_the_count_again(setup: Setup) -> None:
    setup.counters.failure = TIMEOUT
    for _ in range(FAILURES_TO_OPEN - 1):
        setup.count()
        setup.time.advance(REST_AFTER_FAILURE_SECONDS)
    setup.counters.failure = None
    setup.count()
    setup.counters.failure = TIMEOUT

    for _ in range(FAILURES_TO_OPEN - 1):
        setup.count()
        setup.time.advance(REST_AFTER_FAILURE_SECONDS)

    assert "rate_limiter_circuit_open" not in setup.events.names
    assert setup.events.reported[-1][1]["consecutiveFailures"] == FAILURES_TO_OPEN - 1


def test_a_throttled_answer_starts_the_count_again_too(setup: Setup) -> None:
    setup.counters.failure = TIMEOUT
    setup.count()
    setup.time.advance(REST_AFTER_FAILURE_SECONDS)
    setup.counters.failure = THROTTLED
    setup.count()
    setup.counters.failure = TIMEOUT

    setup.count()

    assert setup.events.reported[-1][1]["consecutiveFailures"] == 1


def test_once_open_time_is_over_one_answer_closes_the_circuit(setup: Setup) -> None:
    setup.fail_until_open()
    setup.counters.failure = None
    setup.time.advance(OPEN_SECONDS - REST_AFTER_FAILURE_SECONDS)

    assert setup.count() == 1
    assert setup.count() == 2
    assert setup.counters.calls == FAILURES_TO_OPEN + 2
    assert setup.events.names[-1] == "rate_limiter_circuit_closed"
    assert setup.events.names.count("rate_limiter_circuit_closed") == 1


def test_a_failed_probe_opens_the_circuit_again_at_once(setup: Setup) -> None:
    setup.fail_until_open()
    setup.time.advance(OPEN_SECONDS - REST_AFTER_FAILURE_SECONDS)
    reported = len(setup.events.reported)

    setup.count()

    assert setup.events.names[reported:] == [
        "rate_limiter_unavailable",
        "rate_limiter_circuit_open",
    ]
    setup.time.advance(OPEN_SECONDS - 0.01)
    setup.count()
    assert setup.counters.calls == FAILURES_TO_OPEN + 1


def test_counts_after_the_circuit_closes_come_from_the_store_again(setup: Setup) -> None:
    setup.fail_until_open()
    setup.counters.failure = None
    setup.time.advance(OPEN_SECONDS)

    decision = setup.limiter.check(IP, "alice-sub", USER)

    assert decision.usage == LimitUsage(RateLimitScope.USER, limit=3, remaining=2)
    assert setup.counters.counts == {f"ip#{IP}#{WINDOW}": 1, f"user#alice-sub#{WINDOW}": 1}


# The fallback's own counters: the same conditional count, a bounded number of keys.


def test_local_counters_count_up_to_the_limit_and_then_refuse() -> None:
    counters = LocalCounters()

    counts = [counters.increment("k", limit=3, expires_at=EXPIRES_AT) for _ in range(5)]

    assert counts == [1, 2, 3, None, None]


def test_local_counters_count_keys_apart() -> None:
    counters = LocalCounters()
    counters.increment("first", limit=3, expires_at=EXPIRES_AT)

    assert counters.increment("second", limit=3, expires_at=EXPIRES_AT) == 1


def test_local_counters_drop_the_least_recently_used_key_beyond_their_bound() -> None:
    counters = LocalCounters(max_keys=3)
    for key in ("a", "b", "c"):
        counters.increment(key, limit=10, expires_at=EXPIRES_AT)
    counters.increment("a", limit=10, expires_at=EXPIRES_AT)

    counters.increment("d", limit=10, expires_at=EXPIRES_AT)

    assert len(counters) == 3
    assert counters.increment("a", limit=10, expires_at=EXPIRES_AT) == 3
    assert counters.increment("b", limit=10, expires_at=EXPIRES_AT) == 1


def test_a_client_that_keeps_trying_at_its_limit_keeps_its_counter() -> None:
    """A refused request still counts as use, so hammering can't push a full counter out."""
    counters = LocalCounters(max_keys=2)
    for _ in range(2):
        counters.increment("hammering", limit=2, expires_at=EXPIRES_AT)
    counters.increment("other", limit=2, expires_at=EXPIRES_AT)
    counters.increment("hammering", limit=2, expires_at=EXPIRES_AT)

    counters.increment("newcomer", limit=2, expires_at=EXPIRES_AT)

    assert counters.increment("hammering", limit=2, expires_at=EXPIRES_AT) is None


def test_local_counters_are_bounded_by_default() -> None:
    counters = LocalCounters()

    for number in range(FALLBACK_MAX_KEYS + 1):
        counters.increment(f"user#{number}#w", limit=5, expires_at=EXPIRES_AT)

    assert len(counters) == FALLBACK_MAX_KEYS
    assert counters.increment("user#0#w", limit=5, expires_at=EXPIRES_AT) == 1


def test_the_fallback_is_bounded() -> None:
    setup = Setup(fallback=LocalCounters(max_keys=2))
    setup.counters.failure = TIMEOUT
    for key in ("a", "b", "c"):
        setup.count(key)

    assert setup.count("a") == 1
    assert setup.count("c") == 2


def test_a_new_window_starts_from_zero_in_the_fallback_too() -> None:
    setup = Setup()
    setup.counters.failure = TIMEOUT
    for _ in range(4):
        setup.limiter.check(IP, "alice-sub", USER)
    later = RateLimiter(
        setup.store, SwitchableRateLimitConfig(TINY), FixedClock(NOW + timedelta(seconds=20))
    )

    decision = later.check(IP, "alice-sub", USER)

    assert decision.allowed
    assert decision.usage.remaining == 2
