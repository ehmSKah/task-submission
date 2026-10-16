"""Seed the demo users and print their access tokens (ADR 0010).

This is the one step outside CDK: CloudFormation can't set Cognito user passwords.

Usage:
  python -m orders_api_infra.demo_users seed --env local
  python -m orders_api_infra.demo_users token --env local --role user
"""

import argparse
import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import boto3

from orders_api.domain.access import Role
from orders_api_infra.config import EnvConfig, load_config
from orders_api_infra.endpoints import stack_outputs

if TYPE_CHECKING:
    from mypy_boto3_cognito_idp import CognitoIdentityProviderClient


@dataclass(frozen=True)
class DemoUser:
    name: str
    email: str
    role: Role | None


DEMO_USERS = (
    DemoUser("alice", "alice@example.test", Role.USER),
    DemoUser("bob", "bob@example.test", Role.USER),
    DemoUser("manager", "manager@example.test", Role.ORDER_MANAGER),
    DemoUser("admin", "admin@example.test", Role.ADMIN),
    # A valid token without any recognised role: denied everywhere (ADR 0001).
    DemoUser("nobody", "nobody@example.test", None),
)
ROLE_SHORTCUTS = {"user": "alice", "manager": "manager", "admin": "admin", "none": "nobody"}


def demo_user(name: str) -> DemoUser:
    for user in DEMO_USERS:
        if user.name == name:
            return user
    raise KeyError(f"No demo user '{name}'; choose from {[u.name for u in DEMO_USERS]}")


def demo_password(config: EnvConfig) -> str:
    secret_name = stack_outputs(config).get("SeedUsersSecretName")
    if secret_name is None:
        raise SystemExit(f"Environment '{config.name}' has no seeded users (seedUsers: false)")
    secret = boto3.client("secretsmanager").get_secret_value(SecretId=secret_name)
    password: str = json.loads(secret["SecretString"])["password"]
    return password


def _user_exists(cognito: "CognitoIdentityProviderClient", pool_id: str, email: str) -> bool:
    try:
        cognito.admin_get_user(UserPoolId=pool_id, Username=email)
    except cognito.exceptions.UserNotFoundException:
        return False
    return True


def seed_pool(cognito: "CognitoIdentityProviderClient", pool_id: str, password: str) -> None:
    """Create missing users, and (re)apply every password and group on each run.

    Re-applying makes the script safe to rerun after a partial failure, which would otherwise
    leave a user without its password or group.
    """
    for user in DEMO_USERS:
        if _user_exists(cognito, pool_id, user.email):
            print(f"= {user.name} ({user.email}) exists; reapplying password and group")
        else:
            cognito.admin_create_user(
                UserPoolId=pool_id,
                Username=user.email,
                MessageAction="SUPPRESS",
                UserAttributes=[
                    {"Name": "email", "Value": user.email},
                    {"Name": "email_verified", "Value": "true"},
                ],
            )
            print(f"+ {user.name} ({user.email}) as {user.role.value if user.role else 'no role'}")
        cognito.admin_set_user_password(
            UserPoolId=pool_id, Username=user.email, Password=password, Permanent=True
        )
        if user.role is not None:
            cognito.admin_add_user_to_group(
                UserPoolId=pool_id, Username=user.email, GroupName=user.role.value
            )


def seed(config: EnvConfig) -> None:
    if not config.seed_users:
        raise SystemExit(f"Refusing to seed: environment '{config.name}' has seedUsers: false")
    seed_pool(
        boto3.client("cognito-idp"), stack_outputs(config)["UserPoolId"], demo_password(config)
    )


def access_token(config: EnvConfig, user: DemoUser) -> str:
    response = boto3.client("cognito-idp").initiate_auth(
        ClientId=stack_outputs(config)["UserPoolClientId"],
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": user.email, "PASSWORD": demo_password(config)},
    )
    token: str = response["AuthenticationResult"]["AccessToken"]
    return token


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["seed", "token"])
    parser.add_argument("--env", default="local")
    parser.add_argument("--role", choices=sorted(ROLE_SHORTCUTS), default="user")
    parser.add_argument("--user", help="A demo user by name, e.g. bob (overrides --role)")
    args = parser.parse_args()
    config = load_config(args.env)
    if args.command == "seed":
        seed(config)
    else:
        user = demo_user(args.user or ROLE_SHORTCUTS[args.role])
        sys.stdout.write(access_token(config, user) + "\n")


if __name__ == "__main__":
    main()
