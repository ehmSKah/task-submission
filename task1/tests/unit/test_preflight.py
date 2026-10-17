"""The preflight check: a run reaches the environment it names, and nothing else (ADR 0010)."""

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from orders_api_infra.config import EnvConfig, load_config
from orders_api_infra.preflight import (
    BOOTSTRAP_VERSION_PARAMETER,
    account_report,
    environment_problems,
)

FLOCI = {
    "AWS_ENDPOINT_URL": "http://localhost:4566",
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "AWS_REGION": "us-east-1",
}
AWS = {"AWS_PROFILE": "orders-dev", "AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1"}


@pytest.fixture
def local() -> EnvConfig:
    return load_config("local")


@pytest.fixture
def aws_dev() -> EnvConfig:
    return load_config("aws-dev")


def test_floci_through_its_endpoint_with_dummy_credentials_is_fine(local: EnvConfig) -> None:
    assert environment_problems(local, FLOCI) == []
    assert environment_problems(local, {**FLOCI, "AWS_ENDPOINT_URL": "http://floci:4566"}) == []


@pytest.mark.parametrize(
    "environ",
    [
        {k: v for k, v in FLOCI.items() if k != "AWS_ENDPOINT_URL"},
        {**FLOCI, "AWS_ENDPOINT_URL": "https://dynamodb.us-east-1.amazonaws.com"},
        {**FLOCI, "AWS_ENDPOINT_URL_DYNAMODB": "https://dynamodb.us-east-1.amazonaws.com"},
    ],
    ids=["no endpoint", "endpoint elsewhere", "a service endpoint elsewhere"],
)
def test_floci_refuses_anything_that_could_reach_aws(
    local: EnvConfig, environ: dict[str, str]
) -> None:
    assert environment_problems(local, environ)


def test_aws_with_the_callers_credentials_in_the_environments_region_is_fine(
    aws_dev: EnvConfig,
) -> None:
    assert environment_problems(aws_dev, AWS) == []


@pytest.mark.parametrize(
    ("environ", "named"),
    [
        ({**AWS, "AWS_ENDPOINT_URL": "http://localhost:4566"}, "AWS_ENDPOINT_URL"),
        ({**AWS, "AWS_ENDPOINT_URL_SSM": "http://localhost:4566"}, "AWS_ENDPOINT_URL_SSM"),
        ({**AWS, "AWS_ACCESS_KEY_ID": "test"}, "AWS_ACCESS_KEY_ID"),
        ({**AWS, "AWS_REGION": "eu-central-1"}, "AWS_REGION"),
        ({**AWS, "AWS_DEFAULT_REGION": "eu-west-1"}, "AWS_DEFAULT_REGION"),
        ({"AWS_PROFILE": "orders-dev"}, "AWS_REGION"),
    ],
    ids=[
        "emulator endpoint",
        "service endpoint",
        "Floci's dummy key",
        "region",
        "default region",
        "no region",
    ],
)
def test_aws_refuses_emulator_settings_and_another_region(
    aws_dev: EnvConfig, environ: dict[str, str], named: str
) -> None:
    problems = environment_problems(aws_dev, environ)

    assert len(problems) == 1
    assert named in problems[0]


class Clients:
    """boto3 clients with botocore's Stubber, in place of a session."""

    def __init__(self) -> None:
        self.stubs: dict[str, Stubber] = {}
        self._clients: dict[str, Any] = {}

    def __call__(self, service: str) -> Any:
        if service not in self._clients:
            client = boto3.client(
                service, region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x"
            )
            self._clients[service] = client
            self.stubs[service] = Stubber(client)
            self.stubs[service].activate()
        return self._clients[service]

    def stub(self, service: str) -> Stubber:
        self(service)
        return self.stubs[service]


@pytest.fixture
def clients() -> Iterator[Clients]:
    clients = Clients()
    yield clients
    for stub in clients.stubs.values():
        stub.assert_no_pending_responses()


IDENTITY = {
    "Account": "111122223333",
    "Arn": "arn:aws:sts::111122223333:assumed-role/x/y",
    "UserId": "AROAEXAMPLE:y",
}


def ready(clients: Clients, *, logging_role: bool = True, bootstrapped: bool = True) -> None:
    clients.stub("sts").add_response("get_caller_identity", IDENTITY)
    clients.stub("apigateway").add_response(
        "get_account",
        {"cloudwatchRoleArn": "arn:aws:iam::111122223333:role/apigw-logs"} if logging_role else {},
    )
    ssm = clients.stub("ssm")
    if bootstrapped:
        ssm.add_response(
            "get_parameter",
            {"Parameter": {"Name": BOOTSTRAP_VERSION_PARAMETER, "Value": "29", "Type": "String"}},
            {"Name": BOOTSTRAP_VERSION_PARAMETER},
        )
    else:
        ssm.add_client_error("get_parameter", "ParameterNotFound")


def test_a_ready_account_has_no_problems_and_never_shows_its_id(
    aws_dev: EnvConfig, clients: Clients
) -> None:
    ready(clients)

    problems, notes = account_report(aws_dev, clients)

    assert problems == []
    assert not any("111122223333" in line for line in notes)


def test_missing_credentials_are_a_problem(aws_dev: EnvConfig, clients: Clients) -> None:
    clients.stub("sts").add_client_error("get_caller_identity", "ExpiredToken")

    problems, _ = account_report(aws_dev, clients)

    assert len(problems) == 1
    assert "credentials" in problems[0]


def test_floci_credentials_are_a_problem(aws_dev: EnvConfig, clients: Clients) -> None:
    clients.stub("sts").add_response("get_caller_identity", {**IDENTITY, "Account": "000000000000"})

    problems, _ = account_report(aws_dev, clients)

    assert problems == ["The credentials belong to Floci's account 000000000000, not to AWS"]


def test_access_logs_need_the_accounts_api_gateway_logging_role(
    aws_dev: EnvConfig, clients: Clients
) -> None:
    ready(clients, logging_role=False)

    problems, _ = account_report(aws_dev, clients)

    assert len(problems) == 1
    assert "CloudWatch Logs role" in problems[0]


def test_a_missing_bootstrap_is_a_note_since_make_up_bootstraps(
    aws_dev: EnvConfig, clients: Clients
) -> None:
    ready(clients, bootstrapped=False)

    problems, notes = account_report(aws_dev, clients)

    assert problems == []
    assert any("make bootstrap ENV=aws-dev" in note for note in notes)
