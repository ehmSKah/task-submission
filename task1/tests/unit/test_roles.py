from orders_api.domain.access import Role, recognised_roles


def test_known_groups_become_roles_and_unknown_groups_are_ignored() -> None:
    assert recognised_roles(["USER", "marketing", "ADMIN"]) == {Role.USER, Role.ADMIN}


def test_no_groups_means_no_roles() -> None:
    assert recognised_roles([]) == frozenset()
