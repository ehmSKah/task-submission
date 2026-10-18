"""The retry bounds of ADR 0006: exponential backoff with full jitter, and the start-up budget."""

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from order_consumer.application.retries import RetryBudgetExceeded, RetryPolicy, full_jitter


def longest(cap: float) -> float:
    """A jitter source that always waits as long as allowed."""
    return cap


def test_the_backoff_doubles_from_one_second() -> None:
    policy = RetryPolicy()

    waits = [policy.backoff(retry, longest) for retry in (1, 2, 3, 4)]

    assert waits == [1.0, 2.0, 4.0, 8.0]


def test_the_backoff_is_capped_at_15_seconds() -> None:
    policy = RetryPolicy()

    assert [policy.backoff(retry, longest) for retry in (5, 6, 20)] == [15.0, 15.0, 15.0]


@given(retry=st.integers(min_value=1, max_value=64), source=st.randoms(use_true_random=False))
def test_full_jitter_waits_between_zero_and_the_cap(retry: int, source: random.Random) -> None:
    policy = RetryPolicy()

    wait = policy.backoff(retry, full_jitter(source))

    assert 0.0 <= wait <= min(15.0, 2.0 ** (retry - 1))


def test_full_jitter_spreads_the_waits() -> None:
    jitter = full_jitter(random.Random(42))

    waits = {round(jitter(8.0), 3) for _ in range(20)}

    assert len(waits) > 10, "two instances must not retry in step"


def test_the_worst_case_counts_every_attempt_and_the_longest_wait_before_each_retry() -> None:
    # 5 attempts of 10 s, and before each of the 4 retries a Retry-After of up to 15 s, which is
    # longer than every backoff cap (1, 2, 4, 8 s).
    assert RetryPolicy().worst_case_seconds(attempt_deadline=10.0) == 110.0


def test_the_default_budget_fits_in_half_the_default_poll_interval() -> None:
    RetryPolicy().check_budget(attempt_deadline=10.0, max_poll_interval_ms=300_000)


@pytest.mark.parametrize(
    ("policy", "max_poll_interval_ms"),
    [
        pytest.param(RetryPolicy(max_attempts=10), 300_000, id="more attempts"),
        pytest.param(RetryPolicy(), 200_000, id="a shorter poll interval"),
        pytest.param(RetryPolicy(retry_after_cap=60.0), 300_000, id="a longer Retry-After"),
    ],
)
def test_a_budget_above_half_the_poll_interval_is_refused(
    policy: RetryPolicy, max_poll_interval_ms: int
) -> None:
    with pytest.raises(RetryBudgetExceeded, match=r"half of max\.poll\.interval\.ms"):
        policy.check_budget(attempt_deadline=10.0, max_poll_interval_ms=max_poll_interval_ms)
