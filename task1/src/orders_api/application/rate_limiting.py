"""Rate limiting per client IP, then per user (ADR 0006). Depends only on ports (ADR 0009)."""

from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Protocol

from orders_api.application.orders import Clock
from orders_api.domain.access import Role
from orders_api.domain.rate_limits import (
    LimitUsage,
    RateLimitDecision,
    RateLimits,
    RateLimitScope,
    counter_key,
    ip_subject,
    more_restrictive,
    window_of,
)

EXPIRY_MARGIN = timedelta(seconds=60)
"""How long a counter outlives its window. Expiry only tidies up, since the window is part of the
key; the margin keeps a slow clock from deleting a counter that is still being written."""


class RateLimitCounterStore(Protocol):
    def increment(self, key: str, *, limit: int, expires_at: datetime) -> int | None:
        """One atomic conditional count: if `key`'s counter is below `limit`, add one and return
        the new count; otherwise change nothing and return `None`. The counter may be removed
        once `expires_at` has passed.

        The one call the limiter makes, so a store that fails open, or falls back to another
        store, can wrap this one without the limiter noticing.
        """
        ...


class RateLimitConfigSource(Protocol):
    def current(self) -> RateLimits:
        """The limits in force now; may be cached for a short while."""
        ...


class RateLimiter:
    """Fixed 60-second windows counted in a shared store, per IP first, then per user."""

    def __init__(
        self, counters: RateLimitCounterStore, config: RateLimitConfigSource, clock: Clock
    ) -> None:
        self._counters = counters
        self._config = config
        self._clock = clock

    def check(self, ip: str, subject: str, roles: Collection[Role]) -> RateLimitDecision:
        """Count the request against the client IP's limit, then the user's (`sub`).

        A request over a limit isn't counted by it, so hammering can't push a client's count any
        further, and one over the IP limit never reaches the user's counter.
        """
        now = self._clock.now()
        window = window_of(now)
        limits = self._config.current()
        reset = window.seconds_left(now)
        checks = (
            (RateLimitScope.IP, ip_subject(ip), limits.per_ip),
            (RateLimitScope.USER, subject, limits.per_user(roles)),
        )
        usages = []
        for scope, counted, limit in checks:
            count = self._counters.increment(
                counter_key(scope, counted, window),
                limit=limit,
                expires_at=window.end + EXPIRY_MARGIN,
            )
            if count is None:
                usage = LimitUsage(scope, limit=limit, remaining=0)
                return RateLimitDecision(usage=usage, reset_seconds=reset, exceeded=scope)
            usages.append(LimitUsage(scope, limit=limit, remaining=max(0, limit - count)))
        return RateLimitDecision(usage=more_restrictive(*usages), reset_seconds=reset)
