"""Permissions, and which orders a caller may see (ADR 0001; terms in CONTEXT.md).

Every access check asks about a permission, never about a role or a group. A caller's
permissions are the union over all their recognised roles; without a role they have none.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from types import MappingProxyType

from orders_api.domain.access import Role
from orders_api.domain.order import Order, OrderStatus


class Permission(StrEnum):
    CREATE = "order:create"
    """Always for yourself: the owner is the caller."""
    READ_OWN = "order:read:own"
    READ_ANY = "order:read:any"
    CANCEL_OWN = "order:cancel:own"
    """The status change to `CANCELLED` on your own order only."""
    STATUS_ANY = "order:status:any"
    """Any allowed status change, on any order."""
    DELETE_ANY = "order:delete:any"


_USER = frozenset({Permission.CREATE, Permission.READ_OWN, Permission.CANCEL_OWN})

# ADR 0001's matrix. The roles are nested today, but the union below doesn't rely on that.
ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = MappingProxyType(
    {
        Role.USER: _USER,
        Role.ORDER_MANAGER: _USER | {Permission.READ_ANY, Permission.STATUS_ANY},
        Role.ADMIN: frozenset(Permission),
    }
)


def permissions_of(roles: Iterable[Role]) -> frozenset[Permission]:
    """The union of the permissions of every role; no role means no permission."""
    return frozenset().union(*(ROLE_PERMISSIONS[role] for role in roles))


def permissions_to_change_to(status: OrderStatus) -> frozenset[Permission]:
    """Any one of these lets a caller request a status change to `status` (ADRs 0001 and 0004).

    Depends on the requested status alone, so it is decided before any order is read.
    """
    if status is OrderStatus.CANCELLED:
        return frozenset({Permission.CANCEL_OWN, Permission.STATUS_ANY})
    return frozenset({Permission.STATUS_ANY})


class PermissionDenied(Exception):
    """The caller's roles lack the permission for the action at all: `403`, before any data is
    read (ADR 0001)."""

    def __init__(self, needed: Iterable[Permission]) -> None:
        self.needed = frozenset(needed)
        super().__init__(f"Needs one of: {', '.join(sorted(self.needed))}")


@dataclass(frozen=True)
class Caller:
    """The person behind a request: their subject (`sub`) and their recognised roles."""

    subject: str
    roles: frozenset[Role]

    @cached_property
    def permissions(self) -> frozenset[Permission]:
        return permissions_of(self.roles)

    def require(self, *any_of: Permission) -> None:
        """Pass if the caller holds at least one of the permissions, else `PermissionDenied`."""
        if self.permissions.isdisjoint(any_of):
            raise PermissionDenied(any_of)

    def can_see(self, order: Order) -> bool:
        """Visibility: any order with `order:read:any`, else only their own with `order:read:own`.

        An order the caller can't see is treated exactly like one that doesn't exist (`404`),
        so its existence never leaks (ADR 0001).
        """
        return self._may_act_on(order, any_order=Permission.READ_ANY, own=Permission.READ_OWN)

    def can_change_status_of(self, order: Order) -> bool:
        """Any order with `order:status:any`, else only their own with `order:cancel:own`.

        Someone else's order is then a `404`, exactly like a missing one (ADR 0001).
        """
        return self._may_act_on(order, any_order=Permission.STATUS_ANY, own=Permission.CANCEL_OWN)

    def _may_act_on(self, order: Order, *, any_order: Permission, own: Permission) -> bool:
        """Any order with `any_order`; with only `own`, just the caller's own orders."""
        if any_order in self.permissions:
            return True
        return own in self.permissions and order.owner_id == self.subject
