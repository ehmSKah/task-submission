from orders_api.domain.access import Role, roles_from_groups


def test_known_groups_become_roles_and_unknown_groups_are_ignored() -> None:
    assert roles_from_groups(["USER", "marketing", "ADMIN"]) == {Role.USER, Role.ADMIN}


def test_no_groups_means_no_roles() -> None:
    assert roles_from_groups([]) == frozenset()
