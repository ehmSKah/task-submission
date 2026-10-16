"""Roles a person holds in this API, derived from identity-provider groups (ADR 0001)."""

from collections.abc import Iterable
from enum import StrEnum


class Role(StrEnum):
    USER = "USER"
    ORDER_MANAGER = "ORDER_MANAGER"
    ADMIN = "ADMIN"


def recognised_roles(names: Iterable[str], *, everyone_is_user: bool = False) -> frozenset[Role]:
    """The names that are roles of this API; anything else is ignored.

    Mapping a provider's groups to role names is configuration and happens in the token
    validator (ADR 0001); this only keeps the names the API recognises. `everyone_is_user` is
    ADR 0001's opt-in switch for self-signup: every authenticated person also holds `USER`, so
    nobody is left without a role. It is off by default.
    """
    known = {role.value for role in Role}
    roles = {Role(name) for name in names if name in known}
    if everyone_is_user:
        roles.add(Role.USER)
    return frozenset(roles)
