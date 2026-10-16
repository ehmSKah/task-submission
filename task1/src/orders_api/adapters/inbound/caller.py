"""Who is calling, as established by the authorizer (ADR 0003)."""

from dataclasses import dataclass
from typing import Any

from orders_api.domain.access import Role, recognised_roles


class MissingCallerIdentity(Exception):
    """The authorizer context is absent: a misconfiguration, so the request fails closed."""


@dataclass(frozen=True)
class Caller:
    subject: str
    roles: frozenset[Role]


def caller_from_event(event: dict[str, Any]) -> Caller:
    """Trusted without re-verification: only this API may invoke the function (ADR 0003)."""
    context = (event.get("requestContext") or {}).get("authorizer") or {}
    subject = context.get("sub")
    if not subject:
        raise MissingCallerIdentity
    roles = [role for role in str(context.get("roles") or "").split(",") if role]
    return Caller(subject=str(subject), roles=recognised_roles(roles))
