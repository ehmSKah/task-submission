"""Who is calling, as established by the authorizer (ADR 0003)."""

from typing import Any

from orders_api.domain.access import recognised_roles
from orders_api.domain.permissions import Caller

__all__ = ["Caller", "MissingCallerIdentity", "caller_from_event"]


class MissingCallerIdentity(Exception):
    """The authorizer context is absent: a misconfiguration, so the request fails closed."""


def caller_from_event(event: dict[str, Any]) -> Caller:
    """Trusted without re-verification: only this API may invoke the function (ADR 0003).

    The authorizer has already turned groups into roles (ADR 0001); names that aren't roles of
    this API are dropped again here, so a stray value can never grant a permission.
    """
    context = (event.get("requestContext") or {}).get("authorizer") or {}
    subject = context.get("sub")
    if not subject:
        raise MissingCallerIdentity
    roles = [role for role in str(context.get("roles") or "").split(",") if role]
    return Caller(subject=str(subject), roles=recognised_roles(roles))
