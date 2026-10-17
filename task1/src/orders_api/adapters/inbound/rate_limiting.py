"""Rate limiting at the HTTP edge: who the client is, and what the response says (ADR 0006)."""

from typing import Any

from orders_api.domain.rate_limits import RateLimitDecision, RateLimitScope

RETRY_AFTER_HEADER = "Retry-After"

_WHOSE = {RateLimitScope.IP: "from this IP address", RateLimitScope.USER: "for this user"}


class MissingSourceIp(Exception):
    """The gateway sent no client address: a misconfiguration, so the request fails closed."""


class RateLimitExceeded(Exception):
    """The request ran into a limit: `429`."""

    def __init__(self, decision: RateLimitDecision) -> None:
        super().__init__(f"Rate limit exceeded: {decision.exceeded}")
        self.decision = decision

    @property
    def scope(self) -> RateLimitScope:
        return self.decision.usage.scope

    @property
    def detail(self) -> str:
        usage = self.decision.usage
        return (
            f"Too many requests {_WHOSE[usage.scope]}: the limit is {usage.limit} per minute. "
            f"Retry after {self.decision.reset_seconds} seconds."
        )


def client_ip(event: dict[str, Any]) -> str:
    """The address API Gateway saw the request come from (`requestContext.identity.sourceIp`).

    `X-Forwarded-For` is never read: any client can set it, and the gateway passes it on.
    """
    identity = (event.get("requestContext") or {}).get("identity") or {}
    source_ip = identity.get("sourceIp")
    if not source_ip:
        raise MissingSourceIp
    return str(source_ip)


def rate_limit_headers(decision: RateLimitDecision) -> dict[str, str]:
    """`RateLimit-Limit`, `-Remaining` and `-Reset` (seconds) for the limit the decision names."""
    return {
        "RateLimit-Limit": str(decision.usage.limit),
        "RateLimit-Remaining": str(decision.usage.remaining),
        "RateLimit-Reset": str(decision.reset_seconds),
    }
