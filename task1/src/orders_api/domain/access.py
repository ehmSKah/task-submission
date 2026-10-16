"""Roles a person holds in this API, derived from identity-provider groups (ADR 0001)."""

from collections.abc import Iterable
from enum import StrEnum


class Role(StrEnum):
    USER = "USER"
    ORDER_MANAGER = "ORDER_MANAGER"
    ADMIN = "ADMIN"


def recognised_roles(names: Iterable[str]) -> frozenset[Role]:
    """The names that are roles of this API; anything else is ignored.

    Mapping a provider's groups to role names is configuration and happens in the token
    validator (ADR 0001); this only keeps the names the API recognises.
    """
    known = {role.value for role in Role}
    return frozenset(Role(name) for name in names if name in known)
