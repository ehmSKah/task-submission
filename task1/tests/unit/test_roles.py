from orders_api.domain.access import Role, recognised_roles


def test_known_groups_become_roles_and_unknown_groups_are_ignored() -> None:
    assert recognised_roles(["USER", "marketing", "ADMIN"]) == {Role.USER, Role.ADMIN}


def test_no_groups_means_no_roles() -> None:
    assert recognised_roles([]) == frozenset()


def test_everyone_is_user_is_off_by_default() -> None:
    assert recognised_roles(["marketing"]) == frozenset()


def test_everyone_is_user_gives_a_person_without_a_role_the_user_role() -> None:
    assert recognised_roles([], everyone_is_user=True) == {Role.USER}
    assert recognised_roles(["marketing"], everyone_is_user=True) == {Role.USER}


def test_everyone_is_user_adds_user_to_the_roles_a_person_already_holds() -> None:
    assert recognised_roles(["ADMIN"], everyone_is_user=True) == {Role.USER, Role.ADMIN}
