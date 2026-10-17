"""The whole stack per environment: what production protects, and how dev and prod differ
(ADRs 0010 and 0012)."""

import json
from collections.abc import Iterator
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from orders_api_infra import api as api_module
from orders_api_infra.api import ACCESS_LOG_FIELDS
from orders_api_infra.config import load_config
from orders_api_infra.stack import OrdersApiStack


@pytest.fixture(scope="module", params=["local", "aws-dev", "aws-prod"])
def env(request: pytest.FixtureRequest) -> str:
    name: str = request.param
    return name


@pytest.fixture(scope="module")
def stack(env: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[OrdersApiStack]:
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(api_module, "BUNDLE_DIR", tmp_path_factory.mktemp("bundle"))
        config = load_config(env)
        yield OrdersApiStack(cdk.App(), config.stack_name, config=config)


@pytest.fixture(scope="module")
def template(stack: OrdersApiStack) -> Template:
    return Template.from_stack(stack)


def resource(template: Template, stack: OrdersApiStack, construct: Any) -> dict[str, Any]:
    found: dict[str, Any] = template.to_json()["Resources"][
        stack.get_logical_id(construct.node.default_child)
    ]
    return found


def only(template: Template, resource_type: str) -> dict[str, Any]:
    [found] = template.find_resources(resource_type).values()
    return found


EXPECTED = {
    # env: (account, retains data, log retention, cache seconds, log level, WAF limit, test rule)
    "local": ("000000000000", False, 7, "2", "DEBUG", 3000, 100),
    "aws-dev": (None, False, 14, "2", "INFO", 10000, 100),
    "aws-prod": (None, True, 90, "60", "INFO", 3000, None),
}


def test_aws_environments_deploy_to_the_callers_account_in_the_configured_region(
    env: str, stack: OrdersApiStack
) -> None:
    account = EXPECTED[env][0]

    # Floci's default region locally; Frankfurt for both AWS environments (ADR 0010).
    assert stack.region == ("us-east-1" if env == "local" else "eu-central-1")
    if account is None:
        assert cdk.Token.is_unresolved(stack.account)  # resolved by CloudFormation at deploy
    else:
        assert stack.account == account


def test_only_production_is_termination_protected(env: str, stack: OrdersApiStack) -> None:
    assert stack.termination_protection is EXPECTED[env][1]


def test_production_keeps_and_backs_up_the_orders(
    env: str, stack: OrdersApiStack, template: Template
) -> None:
    retains = EXPECTED[env][1]

    table = resource(template, stack, stack.data.orders_table)

    assert table["DeletionPolicy"] == ("Retain" if retains else "Delete")
    assert table["Properties"]["DeletionProtectionEnabled"] is retains
    assert table["Properties"]["PointInTimeRecoverySpecification"] == {
        "PointInTimeRecoveryEnabled": retains
    }


def test_production_keeps_and_protects_the_user_pool(
    env: str, stack: OrdersApiStack, template: Template
) -> None:
    retains = EXPECTED[env][1]

    pool = resource(template, stack, stack.identity.user_pool)

    assert pool["DeletionPolicy"] == ("Retain" if retains else "Delete")
    assert pool["Properties"].get("DeletionProtection", "INACTIVE") == (
        "ACTIVE" if retains else "INACTIVE"
    )


def test_every_environment_pins_the_user_pool_to_the_lite_tier(
    stack: OrdersApiStack, template: Template
) -> None:
    """The design uses none of Essentials' features, and identity is the largest cost from about
    10 M requests a month (ADR 0013)."""
    pool = resource(template, stack, stack.identity.user_pool)

    assert pool["Properties"]["UserPoolTier"] == "LITE"


def test_log_groups_keep_the_environments_retention(env: str, template: Template) -> None:
    _, retains, days, *_ = EXPECTED[env]

    groups = template.find_resources("AWS::Logs::LogGroup").values()

    assert groups
    for group in groups:
        assert group["Properties"]["RetentionInDays"] == days
        assert group["DeletionPolicy"] == ("Retain" if retains else "Delete")


def test_production_has_no_demo_users_and_no_test_fixtures(
    env: str, stack: OrdersApiStack, template: Template
) -> None:
    for_tests = not EXPECTED[env][1]

    template.resource_count_is("AWS::SecretsManager::Secret", 1 if for_tests else 0)
    template.resource_count_is("AWS::Cognito::UserPool", 2 if for_tests else 1)
    assert (stack.identity.fixtures is not None) is for_tests


def test_functions_run_on_the_environments_architecture_level_and_cache_time(
    env: str, template: Template
) -> None:
    *_, cache_seconds, log_level, _, _ = EXPECTED[env]

    functions = template.find_resources("AWS::Lambda::Function").values()

    assert len(functions) == 2
    for function in functions:
        variables = function["Properties"]["Environment"]["Variables"]
        assert variables["POWERTOOLS_LOG_LEVEL"] == log_level
        if env != "local":  # local builds for the host
            assert function["Properties"]["Architectures"] == ["arm64"]
    [api_function] = [f for f in functions if "RATE_LIMITS_MAX_AGE_SECONDS" in str(f)]
    variables = api_function["Properties"]["Environment"]["Variables"]
    assert variables["RATE_LIMITS_MAX_AGE_SECONDS"] == cache_seconds


def test_only_environments_with_browser_clients_tell_the_function_their_origins(
    env: str, template: Template
) -> None:
    """aws-prod's function is configured as it was before CORS existed (ADR 0014)."""
    [api_function] = [
        function
        for function in template.find_resources("AWS::Lambda::Function").values()
        if "RATE_LIMITS_MAX_AGE_SECONDS" in str(function)
    ]
    variables = api_function["Properties"]["Environment"]["Variables"]

    if env == "aws-prod":
        assert "CORS_ALLOWED_ORIGINS" not in variables
    else:
        assert variables["CORS_ALLOWED_ORIGINS"] == "http://localhost:8080"


def test_waf_limits_follow_the_environment(env: str, template: Template) -> None:
    *_, flood_limit, test_limit = EXPECTED[env]

    rules = {
        rule["Name"]: rule for rule in only(template, "AWS::WAFv2::WebACL")["Properties"]["Rules"]
    }

    assert rules["per-ip-flood"]["Statement"]["RateBasedStatement"]["Limit"] == flood_limit
    if test_limit is None:
        assert "per-ip-test" not in rules
    else:
        assert rules["per-ip-test"]["Statement"]["RateBasedStatement"]["Limit"] == test_limit
    template.resource_count_is("AWS::WAFv2::WebACLAssociation", 1)


def test_access_logs_are_written_as_json_on_aws_only(env: str, template: Template) -> None:
    stage = only(template, "AWS::ApiGateway::Stage")["Properties"]

    if env == "local":
        assert "AccessLogSetting" not in stage
        return
    setting = stage["AccessLogSetting"]
    [group_id] = [
        group for group in template.find_resources("AWS::Logs::LogGroup") if "AccessLogs" in group
    ]
    assert setting["DestinationArn"] == {"Fn::GetAtt": [group_id, "Arn"]}
    assert json.loads(setting["Format"]) == ACCESS_LOG_FIELDS


def test_access_log_lines_carry_no_header_and_so_no_token() -> None:
    line = json.dumps(ACCESS_LOG_FIELDS).lower()

    assert "authorization" not in line
    assert "header" not in line
    assert "$input" not in line
    assert "apikey" not in line
    assert {"requestId", "status", "responseLatency", "sourceIp", "authorizerError"} <= set(
        ACCESS_LOG_FIELDS
    )
