"""Rate limits: fixed windows, counter keys and role tiers (ADR 0006).

Requests are counted per client IP and per user in fixed 60-second windows aligned to the minute
(UTC). Each window has its own counter, named by its key, so a new window starts from zero
without anything being reset.
"""

import ipaddress
import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from orders_api.domain.access import Role

WINDOW = timedelta(seconds=60)
IPV6_PREFIX = 64
"""One host or user typically controls a whole IPv6 `/64`, so that is what gets counted."""
LOGGED_IPV4_PREFIX = 24


class RateLimitScope(StrEnum):
    """What a limit counts: requests from one client IP, or from one user (`sub`)."""

    IP = "ip"
    USER = "user"


class InvalidRateLimits(ValueError):
    """A rate-limit configuration that can't be enforced as given."""


@dataclass(frozen=True)
class RateLimits:
    """Requests allowed per window: per client IP, and per user by role tier."""

    per_ip: int
    per_role: Mapping[Role, int] = field(hash=False)

    def __post_init__(self) -> None:
        # A private, read-only copy: the caller's mapping can't change the limits later.
        object.__setattr__(self, "per_role", MappingProxyType(dict(self.per_role)))
        missing = sorted(role.value for role in Role if role not in self.per_role)
        if missing:
            raise InvalidRateLimits(f"No per-user limit for {', '.join(missing)}")
        if min(self.per_ip, *self.per_role.values()) < 1:
            raise InvalidRateLimits("Every limit must allow at least one request")

    def per_user(self, roles: Collection[Role]) -> int:
        """The highest tier among the caller's roles, since roles combine (ADR 0001).

        A caller without a recognised role still gets a budget, though every order endpoint
        answers them `403`: the lowest tier, so having no role is never worth more than any role.
        """
        if not roles:
            return min(self.per_role.values())
        return max(self.per_role[role] for role in roles)


DEFAULT_RATE_LIMITS = RateLimits(
    per_ip=300,
    per_role={Role.USER: 60, Role.ORDER_MANAGER: 300, Role.ADMIN: 300},
)
"""ADR 0006's limits: what a new environment starts with, and the fallback while no valid
configuration has been read."""


@dataclass(frozen=True)
class Window:
    """One fixed 60-second window, starting on a full minute (UTC)."""

    start: datetime

    @property
    def end(self) -> datetime:
        return self.start + WINDOW

    @property
    def label(self) -> str:
        """The window's start to the minute, e.g. `2026-10-17T09:31Z`."""
        return self.start.strftime("%Y-%m-%dT%H:%MZ")

    def seconds_left(self, now: datetime) -> int:
        """Whole seconds until the next window, rounded up: at least 1 while this one lasts."""
        return max(1, math.ceil((self.end - now).total_seconds()))


def window_of(now: datetime) -> Window:
    """The window `now` falls in."""
    return Window(start=now.astimezone(UTC).replace(second=0, microsecond=0))


def counter_key(scope: RateLimitScope, subject: str, window: Window) -> str:
    """`<scope>#<subject>#<windowStart>`, e.g. `ip#203.0.113.7#2026-10-17T09:31Z`."""
    return f"{scope.value}#{subject}#{window.label}"


def _address(source_ip: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address, with an IPv4-mapped IPv6 address taken as the IPv4 address it carries."""
    try:
        address = ipaddress.ip_address(source_ip)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def ip_subject(source_ip: str) -> str:
    """Who the per-IP limit counts: an IPv4 address, or the `/64` an IPv6 address is in.

    The gateway sets the source IP, so anything else is unexpected; it is counted as given.
    """
    address = _address(source_ip)
    if address is None:
        return source_ip
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.IPv6Network((address, IPV6_PREFIX), strict=False))
    return str(address)


def logged_network(source_ip: str) -> str | None:
    """The address truncated for logs (IPv4 `/24`, IPv6 `/64`), since IP addresses are personal
    data (ADR 0012); `None` for anything that isn't an address."""
    address = _address(source_ip)
    if address is None:
        return None
    prefix = IPV6_PREFIX if isinstance(address, ipaddress.IPv6Address) else LOGGED_IPV4_PREFIX
    return str(ipaddress.ip_network((address, prefix), strict=False))


@dataclass(frozen=True)
class LimitUsage:
    """One limit as the request left it: how many requests it allows, and how many are left."""

    scope: RateLimitScope
    limit: int
    remaining: int


def more_restrictive(first: LimitUsage, second: LimitUsage) -> LimitUsage:
    """The limit the client will meet first: fewer requests left, then the smaller limit.

    Both windows end together, so the reset never decides. On a full tie, `first`.
    """
    return min(first, second, key=lambda usage: (usage.remaining, usage.limit))


@dataclass(frozen=True)
class RateLimitDecision:
    """Whether a request may proceed, and what its `RateLimit-*` headers say."""

    usage: LimitUsage
    """The limit the headers describe: the one exceeded, else the more restrictive one."""
    reset_seconds: int
    """Seconds until the window ends and the counts start again."""
    exceeded: RateLimitScope | None = None
    """The limit the request ran into, if any: it is answered `429`."""

    @property
    def allowed(self) -> bool:
        return self.exceeded is None
