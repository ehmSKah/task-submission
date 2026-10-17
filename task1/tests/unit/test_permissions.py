"""The permission policy of ADR 0001: the matrix, the union, no role, and who sees which order."""

import pytest

from orders_api.domain.access import Role
from orders_api.domain.order import OrderStatus
from orders_api.domain.permissions import (
    Caller,
    Permission,
    PermissionDenied,
    permissions_of,
    permissions_to_change_to,
)
from tests.builders import an_order

# ADR 0001's matrix, written out cell by cell so a change to the policy shows up here.
MATRIX = {
    Permission.CREATE: {Role.USER, Role.ORDER_MANAGER, Role.ADMIN},
    Permission.READ_OWN: {Role.USER, Role.ORDER_MANAGER, Role.ADMIN},
    Permission.READ_ANY: {Role.ORDER_MANAGER, Role.ADMIN},
    Permission.CANCEL_OWN: {Role.USER, Role.ORDER_MANAGER, Role.ADMIN},
    Permission.STATUS_ANY: {Role.ORDER_MANAGER, Role.ADMIN},
    Permission.DELETE_ANY: {Role.ADMIN},
}


def test_the_permissions_carry_their_published_names() -> None:
    assert {permission.value for permission in Permission} == {
        "order:create",
        "order:read:own",
        "order:read:any",
        "order:cancel:own",
        "order:status:any",
        "order:delete:any",
    }


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("permission", list(Permission))
def test_each_cell_of_the_matrix(role: Role, permission: Permission) -> None:
    assert (permission in permissions_of({role})) == (role in MATRIX[permission])


def test_admin_has_every_permission() -> None:
    assert permissions_of({Role.ADMIN}) == frozenset(Permission)


def test_several_roles_get_the_union_of_their_permissions() -> None:
    assert permissions_of({Role.USER, Role.ORDER_MANAGER}) == permissions_of({Role.ORDER_MANAGER})
    assert permissions_of({Role.USER, Role.ADMIN}) == frozenset(Permission)


def test_no_role_means_no_permission() -> None:
    assert permissions_of(set()) == frozenset()


def caller(*roles: Role, subject: str = "alice-sub") -> Caller:
    return Caller(subject=subject, roles=frozenset(roles))


def test_a_caller_holding_one_of_the_permissions_passes() -> None:
    caller(Role.USER).require(Permission.READ_OWN, Permission.READ_ANY)


def test_a_caller_without_any_of_the_permissions_is_denied() -> None:
    with pytest.raises(PermissionDenied):
        caller(Role.USER).require(Permission.DELETE_ANY)


@pytest.mark.parametrize("permission", list(Permission))
def test_a_caller_without_a_role_is_denied_everything(permission: Permission) -> None:
    with pytest.raises(PermissionDenied):
        caller().require(permission)


def test_an_owner_with_read_own_sees_their_order() -> None:
    assert caller(Role.USER, subject="alice-sub").can_see(an_order(owner_id="alice-sub"))


def test_someone_else_with_only_read_own_does_not_see_the_order() -> None:
    assert not caller(Role.USER, subject="bob-sub").can_see(an_order(owner_id="alice-sub"))


@pytest.mark.parametrize("role", [Role.ORDER_MANAGER, Role.ADMIN])
def test_read_any_sees_everyone_s_orders(role: Role) -> None:
    assert caller(role, subject="staff-sub").can_see(an_order(owner_id="alice-sub"))


def test_without_a_read_permission_not_even_an_own_order_is_visible() -> None:
    assert not caller(subject="alice-sub").can_see(an_order(owner_id="alice-sub"))


# Status changes: what they need, decided by the requested status alone, and on whose orders.


def test_cancelling_needs_cancel_own_or_status_any() -> None:
    assert permissions_to_change_to(OrderStatus.CANCELLED) == {
        Permission.CANCEL_OWN,
        Permission.STATUS_ANY,
    }


@pytest.mark.parametrize("status", [s for s in OrderStatus if s is not OrderStatus.CANCELLED])
def test_every_other_status_needs_status_any(status: OrderStatus) -> None:
    assert permissions_to_change_to(status) == {Permission.STATUS_ANY}


def test_an_owner_with_cancel_own_may_change_the_status_of_their_own_order() -> None:
    assert caller(Role.USER).can_change_status_of(an_order(owner_id="alice-sub"))


def test_someone_else_with_only_cancel_own_may_not_change_the_status_of_the_order() -> None:
    assert not caller(Role.USER, subject="bob-sub").can_change_status_of(
        an_order(owner_id="alice-sub")
    )


@pytest.mark.parametrize("role", [Role.ORDER_MANAGER, Role.ADMIN])
def test_status_any_may_change_the_status_of_everyone_s_orders(role: Role) -> None:
    assert caller(role, subject="staff-sub").can_change_status_of(an_order(owner_id="alice-sub"))


def test_without_a_status_permission_not_even_an_own_order_may_change() -> None:
    assert not caller(subject="alice-sub").can_change_status_of(an_order(owner_id="alice-sub"))
