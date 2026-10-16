"""Roles a person holds in this API, derived from identity-provider groups (ADR 0001)."""

from collections.abc import Iterable
from enum import StrEnum


class Role(StrEnum):
    USER = "USER"
    ORDER_MANAGER = "ORDER_MANAGER"
    ADMIN = "ADMIN"


def roles_from_groups(groups: Iterable[str]) -> frozenset[Role]:
    """Groups the API doesn't know are ignored."""
    known = {role.value for role in Role}
    return frozenset(Role(group) for group in groups if group in known)
