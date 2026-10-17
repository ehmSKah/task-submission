"""`make http-env` and the `.http` collection that reads its file."""

import json
import re
import stat
import subprocess
from pathlib import Path

import pytest

from orders_api_infra import http_env
from orders_api_infra.demo_users import DEMO_USERS
from orders_api_infra.http_env import ENV_FILE, environment, merged, write
from orders_api_infra.paths import PROJECT_DIR

TOKENS = {user.name: f"token-of-{user.name}" for user in DEMO_USERS}
COLLECTION = sorted((PROJECT_DIR / "http").glob("*.http"))


def test_an_environment_holds_the_api_url_and_one_token_per_demo_user() -> None:
    values = environment("http://api.test/live", TOKENS)

    assert values == {
        "baseUrl": "http://api.test/live",
        "aliceToken": "token-of-alice",
        "bobToken": "token-of-bob",
        "managerToken": "token-of-manager",
        "adminToken": "token-of-admin",
        "nobodyToken": "token-of-nobody",
    }


def test_writing_one_environment_keeps_the_others() -> None:
    existing = json.dumps({"aws-dev": {"baseUrl": "https://aws"}, "local": {"baseUrl": "old"}})

    result = merged(existing, "local", {"baseUrl": "new"})

    assert result == {"aws-dev": {"baseUrl": "https://aws"}, "local": {"baseUrl": "new"}}


def test_a_missing_file_starts_empty() -> None:
    assert merged(None, "local", {"baseUrl": "new"}) == {"local": {"baseUrl": "new"}}


def test_the_file_is_readable_by_its_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "http-client.private.env.json"

    write(path, "local", {"baseUrl": "u", "aliceToken": "t"})
    write(path, "aws-dev", {"baseUrl": "v"})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {
        "local": {"baseUrl": "u", "aliceToken": "t"},
        "aws-dev": {"baseUrl": "v"},
    }


def test_a_world_readable_file_is_tightened_when_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "http-client.private.env.json"
    path.write_text("{}")
    path.chmod(0o644)

    write(path, "local", {"baseUrl": "u"})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_env_file_is_git_ignored() -> None:
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", str(ENV_FILE)],
        cwd=PROJECT_DIR,
        check=False,
    )

    assert ignored.returncode == 0, f"{ENV_FILE} must be in .gitignore: it holds tokens"


def test_the_summary_names_the_users_but_prints_no_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    http_env.report(tmp_path / "env.json", "local", environment("http://api.test", TOKENS))

    printed = capsys.readouterr().out
    assert "token-of" not in printed
    assert "alice" in printed
    assert "make http-env" in printed  # how to refresh expired tokens


def test_there_is_a_collection_for_orders_errors_and_rate_limits() -> None:
    assert [path.name for path in COLLECTION] == ["errors.http", "orders.http", "rate-limit.http"]


@pytest.mark.parametrize("path", COLLECTION, ids=lambda path: path.name)
def test_every_variable_the_collection_uses_is_provided(path: Path) -> None:
    """From the env file, or set by a response handler earlier in the same file."""
    provided = set(environment("u", TOKENS))
    for line in path.read_text().splitlines():
        for name in re.findall(r"\{\{\s*(\w+)\s*}}", line):
            assert name in provided, f"{path.name}: {{{{{name}}}}} is used before it is set"
        provided |= set(re.findall(r'client\.global\.set\("(\w+)"', line))


@pytest.mark.parametrize("path", COLLECTION, ids=lambda path: path.name)
def test_the_collection_holds_no_token(path: Path) -> None:
    assert "eyJ" not in path.read_text()
