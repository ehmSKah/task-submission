"""Write the `.http` collection's private environment file: the API URL and a token per demo user.

The file is in the JetBrains HTTP Client format (`http/http-client.private.env.json`), which
IntelliJ IDEA, PyCharm and httpyac (VS Code, or its command line) read. It holds live tokens,
so it is git-ignored and readable by its owner only. Access tokens are short-lived (ADR 0010);
running it again refreshes them. Other environments in the file are kept.

Usage: python -m orders_api_infra.http_env --env local
"""

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orders_api_infra import endpoints
from orders_api_infra.demo_users import DEMO_USERS, access_token
from orders_api_infra.identity import TOKEN_VALIDITY_MINUTES
from orders_api_infra.paths import PROJECT_DIR

ENV_FILE = PROJECT_DIR / "http" / "http-client.private.env.json"


def environment(api_url: str, tokens: Mapping[str, str]) -> dict[str, str]:
    """One environment's variables: `baseUrl`, and `<user>Token` per demo user."""
    return {"baseUrl": api_url, **{f"{user}Token": token for user, token in tokens.items()}}


def merged(existing: str | None, name: str, environment: Mapping[str, str]) -> dict[str, Any]:
    """The file's environments with the one called `name` replaced by `environment`."""
    environments: dict[str, Any] = json.loads(existing) if existing else {}
    return {**environments, name: dict(environment)}


def write(path: Path, name: str, environment: Mapping[str, str]) -> None:
    """Replace the environment called `name` in the file at `path`, which only its owner may
    read."""
    existing = path.read_text() if path.is_file() else None
    content = json.dumps(merged(existing, name, environment), indent=2) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)  # also for a file that existed with wider permissions
    with os.fdopen(descriptor, "w") as file:
        file.write(content)


def report(path: Path, name: str, environment: Mapping[str, str]) -> None:
    """Say what was written, without any token."""
    users = ", ".join(key.removesuffix("Token") for key in environment if key.endswith("Token"))
    print(
        f"Wrote {path.relative_to(PROJECT_DIR) if path.is_relative_to(PROJECT_DIR) else path}: "
        f"environment '{name}' with baseUrl {environment['baseUrl']} and tokens for {users}.\n"
        f"The tokens expire after {TOKEN_VALIDITY_MINUTES} minutes; run `make http-env` again "
        "for fresh ones."
    )


def main() -> None:
    config, api_url = endpoints.deployed_api(__doc__)
    tokens = {user.name: access_token(config, user) for user in DEMO_USERS}
    variables = environment(api_url, tokens)
    write(ENV_FILE, config.name, variables)
    report(ENV_FILE, config.name, variables)


if __name__ == "__main__":
    main()
