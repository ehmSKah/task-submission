import pytest

from orders_api.domain.access import Role
from orders_api_infra.config import EnvConfig, load_config
from orders_api_infra.demo_users import DEMO_USERS, ROLE_SHORTCUTS, demo_user, seed


def test_seeding_is_refused_where_the_environment_forbids_it() -> None:
    local = load_config("local")
    production_like = EnvConfig.model_validate(
        {**local.model_dump(by_alias=True), "seedUsers": False}
    )

    with pytest.raises(SystemExit, match="Refusing to seed"):
        seed(production_like)


def test_there_is_one_demo_user_per_role_plus_a_second_owner_and_one_without_a_role() -> None:
    roles = [user.role for user in DEMO_USERS]

    assert roles.count(Role.USER) == 2
    assert roles.count(Role.ORDER_MANAGER) == 1
    assert roles.count(Role.ADMIN) == 1
    assert roles.count(None) == 1


def test_role_shortcuts_name_existing_users() -> None:
    for name in ROLE_SHORTCUTS.values():
        assert demo_user(name).name == name


class RecordingCognito:
    """Records the admin calls the seed script makes, for a pool that already has alice."""

    class exceptions:
        class UserNotFoundException(Exception):
            pass

    def __init__(self, existing: set[str]) -> None:
        self.existing = existing
        self.calls: list[tuple[str, str]] = []

    def admin_get_user(self, *, UserPoolId: str, Username: str) -> dict[str, str]:
        if Username not in self.existing:
            raise self.exceptions.UserNotFoundException
        return {}

    def admin_create_user(self, **kwargs: object) -> None:
        self.calls.append(("create", str(kwargs["Username"])))

    def admin_set_user_password(self, **kwargs: object) -> None:
        self.calls.append(("password", str(kwargs["Username"])))

    def admin_add_user_to_group(self, **kwargs: object) -> None:
        self.calls.append(("group", str(kwargs["Username"])))


def test_seeding_repairs_users_a_previous_run_left_half_done() -> None:
    from orders_api_infra.demo_users import seed_pool

    cognito = RecordingCognito(existing={"alice@example.test"})

    seed_pool(cognito, "pool", "pw")  # type: ignore[arg-type]

    assert ("create", "alice@example.test") not in cognito.calls
    assert ("password", "alice@example.test") in cognito.calls
    assert ("group", "alice@example.test") in cognito.calls
    assert ("create", "bob@example.test") in cognito.calls
    assert ("group", "nobody@example.test") not in cognito.calls
