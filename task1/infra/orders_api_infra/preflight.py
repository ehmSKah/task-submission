"""Check that a run reaches the environment it names, and nothing else (ADR 0010).

Every make target that talks to an environment runs the environment check first: a Floci
environment talks to the emulator only, an AWS environment never to an emulator or an endpoint
override, and in its own region. `--account` (run by `make doctor` for AWS environments) also asks
AWS whether the account is ready: credentials, the API Gateway logging role, CDK's bootstrap.

Usage: python -m orders_api_infra.preflight --env aws-dev [--account]
"""

import argparse
import os
import sys
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from orders_api_infra.config import EnvConfig, load_config

FLOCI_ACCOUNT = "000000000000"
FLOCI_ACCESS_KEY = "test"
EMULATOR_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "floci", "localhost.floci.io"})
ENDPOINT_VARIABLE = "AWS_ENDPOINT_URL"
REGION_VARIABLES = ("AWS_REGION", "AWS_DEFAULT_REGION")
# Where `cdk bootstrap` records its version (the default qualifier).
BOOTSTRAP_VERSION_PARAMETER = "/cdk-bootstrap/hnb659fds/version"


def _endpoint_variables(environ: Mapping[str, str]) -> list[str]:
    """`AWS_ENDPOINT_URL` and the service-specific `AWS_ENDPOINT_URL_<SERVICE>` overrides."""
    return sorted(
        name
        for name in environ
        if name == ENDPOINT_VARIABLE or name.startswith(f"{ENDPOINT_VARIABLE}_")
    )


def environment_problems(config: EnvConfig, environ: Mapping[str, str]) -> list[str]:
    """What in `environ` would send this run anywhere but `config`'s target. Empty when none."""
    endpoints = _endpoint_variables(environ)
    if config.target == "floci":
        problems = [
            f"{name}={environ[name]} is not the Floci emulator"
            for name in endpoints
            if urlparse(environ[name]).hostname not in EMULATOR_HOSTS
        ]
        if ENDPOINT_VARIABLE not in environ:
            problems.append(f"{ENDPOINT_VARIABLE} must point at Floci for '{config.name}'")
        return problems
    problems = [
        f"{name} is set: AWS environment '{config.name}' never uses an endpoint override"
        for name in endpoints
    ]
    if environ.get("AWS_ACCESS_KEY_ID") == FLOCI_ACCESS_KEY:
        problems.append("AWS_ACCESS_KEY_ID is Floci's dummy key, not the caller's credentials")
    regions = {name: environ[name] for name in REGION_VARIABLES if name in environ}
    if not regions:
        problems.append(f"AWS_REGION is not set; '{config.name}' lives in {config.region}")
    problems += [
        f"{name} is {region}, but '{config.name}' lives in {config.region}"
        for name, region in regions.items()
        if region != config.region
    ]
    return problems


def account_report(config: EnvConfig, client: Callable[[str], Any]) -> tuple[list[str], list[str]]:
    """Problems that stop a deploy to `config`'s account, and notes. Neither shows the account
    id or the caller's identity, so the output is safe in CI logs."""
    try:
        account = client("sts").get_caller_identity()["Account"]
    except (BotoCoreError, ClientError) as error:
        return [
            f"No usable AWS credentials ({type(error).__name__}): sign in or set AWS_PROFILE"
        ], []
    if account == FLOCI_ACCOUNT:
        return [f"The credentials belong to Floci's account {FLOCI_ACCOUNT}, not to AWS"], []
    problems: list[str] = []
    notes = ["AWS credentials resolve (account and caller not shown)"]
    if config.access_logs and not client("apigateway").get_account().get("cloudwatchRoleArn"):
        problems.append(
            f"API Gateway has no CloudWatch Logs role in {config.region}, which access logs "
            "need: set it once per account and region (docs/aws.md)"
        )
    try:
        client("ssm").get_parameter(Name=BOOTSTRAP_VERSION_PARAMETER)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ParameterNotFound":
            raise
        notes.append(
            f"CDK is not bootstrapped in {config.region}: make bootstrap ENV={config.name} "
            "(make up does it)"
        )
    else:
        notes.append(f"CDK is bootstrapped in {config.region}")
    return problems, notes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="local")
    parser.add_argument("--account", action="store_true", help="also check the AWS account")
    args = parser.parse_args()
    config = load_config(args.env)
    problems = environment_problems(config, os.environ)
    notes: list[str] = []
    if not problems and args.account and config.target == "aws":
        session = boto3.Session(region_name=config.region)

        def client(service: str) -> Any:
            # boto3's stubs type each service name; any name is fine here.
            return session.client(service)  # type: ignore[call-overload]

        problems, notes = account_report(config, client)
    for note in notes:
        print(f"✓ {note}")
    for problem in problems:
        print(f"✗ {problem}", file=sys.stderr)
    if problems:
        raise SystemExit(f"Refusing to run against '{config.name}' (ADR 0010)")


if __name__ == "__main__":
    main()
