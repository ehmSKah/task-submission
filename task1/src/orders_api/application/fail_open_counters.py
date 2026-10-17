"""A counter store that fails open without rewarding abuse (ADR 0006).

It wraps the real counter store behind the same port, so the limiter doesn't notice:
- a throttled counter is over the limit: the request is answered `429`
- an outage (timeout, network or service error) lets the request through, counted instead in
  this instance's memory with the same limits and keys: a fixed window per Lambda instance
- after a failed call the store rests for a second, so a request whose IP check failed doesn't
  wait for its user check too: a request pays for one failed call at most
- after a few consecutive failures the circuit opens: the store rests for 30 seconds and only
  the memory counts. The first call after that is a probe; an answer closes the circuit, a
  failure opens it again at once

Operational events (ADR 0012) go to an injected callback, so this module needs no logger.
"""

import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from orders_api.application.rate_limiting import (
    CounterStoreThrottled,
    CounterStoreUnavailable,
    RateLimitCounterStore,
)

FAILURES_TO_OPEN = 3
"""Consecutive failed calls that open the circuit. One failure alone may be a blip."""
OPEN_SECONDS = 30.0
"""How long an open circuit leaves the store alone before probing it."""
REST_AFTER_FAILURE_SECONDS = 1.0
"""How long any failed call leaves the store alone. Ample to cover the rest of its request."""
FALLBACK_MAX_KEYS = 10_000
"""Counters kept in memory at most: a few MB, and more than one instance meets in a window."""

TimeSource = Callable[[], float]
"""Monotonic seconds."""


class RateLimiterEvents(Protocol):
    def __call__(self, event: str, /, **fields: object) -> None:
        """Report `rate_limiter_unavailable`, `rate_limiter_circuit_open` or
        `rate_limiter_circuit_closed` with its details. Never a counter key: keys carry the
        client's full address."""
        ...


def no_events(event: str, /, **fields: object) -> None:
    """The default: report nothing."""


class LocalCounters:
    """A `RateLimitCounterStore` in this instance's memory: the same conditional count, for at
    most `max_keys` counters, the least recently used dropped first.

    Every count is a use, a refused one included, so a client hammering at its limit keeps its
    full counter. Expiry needs no clock: the window is part of the key, so an old window's
    counters are never touched again and are the first to go.
    """

    def __init__(self, max_keys: int = FALLBACK_MAX_KEYS) -> None:
        self._counts: OrderedDict[str, int] = OrderedDict()
        self._max_keys = max_keys

    def __len__(self) -> int:
        return len(self._counts)

    def increment(self, key: str, *, limit: int, expires_at: datetime) -> int | None:
        count = self._counts.pop(key, 0)
        counted = count < limit
        self._counts[key] = count + 1 if counted else count
        while len(self._counts) > self._max_keys:
            self._counts.popitem(last=False)
        return count + 1 if counted else None


class _CircuitBreaker:
    """Whether to ask the store now, from how its recent calls went."""

    def __init__(self, time_source: TimeSource) -> None:
        self._now = time_source
        self._failures = 0
        self._resting_until = float("-inf")

    @property
    def failures(self) -> int:
        return self._failures

    def allows_call(self) -> bool:
        return self._now() >= self._resting_until

    def answered(self) -> bool:
        """Record an answer; whether it closed an open circuit."""
        was_open = self._failures >= FAILURES_TO_OPEN
        self._failures = 0
        return was_open

    def failed(self) -> bool:
        """Record a failed call; whether the circuit is open now (again, after a probe)."""
        self._failures += 1
        opened = self._failures >= FAILURES_TO_OPEN
        rest = OPEN_SECONDS if opened else REST_AFTER_FAILURE_SECONDS
        self._resting_until = self._now() + rest
        return opened


class FailOpenCounterStore:
    """A `RateLimitCounterStore` around another one, failing open into `LocalCounters`."""

    def __init__(
        self,
        store: RateLimitCounterStore,
        *,
        events: RateLimiterEvents = no_events,
        time_source: TimeSource = time.monotonic,
        fallback: LocalCounters | None = None,
    ) -> None:
        self._store = store
        self._events = events
        self._breaker = _CircuitBreaker(time_source)
        self._fallback = fallback if fallback is not None else LocalCounters()

    def increment(self, key: str, *, limit: int, expires_at: datetime) -> int | None:
        """The store's count; `None` (over the limit) when it is throttled; the fallback's count
        while it is unavailable or resting. Any other error propagates."""
        if self._breaker.allows_call():
            try:
                count = self._store.increment(key, limit=limit, expires_at=expires_at)
            except CounterStoreThrottled:
                self._answered()
                return None
            except CounterStoreUnavailable as error:
                self._failed(error)
            else:
                self._answered()
                return count
        return self._fallback.increment(key, limit=limit, expires_at=expires_at)

    def _answered(self) -> None:
        if self._breaker.answered():
            self._events("rate_limiter_circuit_closed")

    def _failed(self, error: CounterStoreUnavailable) -> None:
        opened = self._breaker.failed()
        failures = self._breaker.failures
        self._events(
            "rate_limiter_unavailable", errorType=error.error_type, consecutiveFailures=failures
        )
        if opened:
            self._events(
                "rate_limiter_circuit_open",
                errorType=error.error_type,
                consecutiveFailures=failures,
                openSeconds=OPEN_SECONDS,
            )
