"""The rate-limit counter table and configuration parameter, and the API function's access to
them (ADRs 0005, 0006 and 0012)."""

import json
from pathlib import Path
from typing import Any

import pytest
from aws_cdk.assertions import Match

from orders_api.adapters.outbound.ssm_rate_limits import rate_limits_from_json
from orders_api.domain.rate_limits import DEFAULT_RATE_LIMITS
from tests.unit.data_stack import as_list, config, synth


def policy_statements(template: Any) -> list[dict[str, Any]]:
    return [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


@pytest.mark.parametrize("removal_policy", ["destroy", "retain"])
def test_the_counter_table_expires_counters_and_is_always_disposable(
    removal_policy: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, data, template = synth(config(removalPolicy=removal_policy), tmp_path, monkeypatch)

    table_id = stack.get_logical_id(data.rate_limit_table.node.default_child)  # type: ignore[arg-type]
    table = template.to_json()["Resources"][table_id]
    assert table["DeletionPolicy"] == "Delete"
    assert table["Properties"]["KeySchema"] == [{"AttributeName": "id", "KeyType": "HASH"}]
    assert table["Properties"]["BillingMode"] == "PAY_PER_REQUEST"
    assert table["Properties"]["TimeToLiveSpecification"] == {
        "AttributeName": "expiresAt",
        "Enabled": True,
    }
    assert table["Properties"]["PointInTimeRecoverySpecification"] == {
        "PointInTimeRecoveryEnabled": False
    }
    assert "StreamSpecification" not in table["Properties"]
    template.has_output("RateLimitTableName", {})


def test_the_parameter_is_named_per_environment_and_starts_with_the_default_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, template = synth(config(name="dev"), tmp_path, monkeypatch)

    [parameter] = template.find_resources("AWS::SSM::Parameter").values()

    assert parameter["Properties"]["Name"] == "/orders-api/dev/rate-limits"
    assert parameter["Properties"]["Type"] == "String"
    assert rate_limits_from_json(parameter["Properties"]["Value"]) == DEFAULT_RATE_LIMITS
    template.has_output("RateLimitsParameterName", {})


def test_the_api_function_may_only_count_in_the_counter_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, data, template = synth(config(), tmp_path, monkeypatch)
    table_id = stack.get_logical_id(data.rate_limit_table.node.default_child)  # type: ignore[arg-type]
    table_arn = {"Fn::GetAtt": [table_id, "Arn"]}

    statements = [s for s in policy_statements(template) if table_arn in as_list(s["Resource"])]

    assert len(statements) == 1
    assert statements[0]["Action"] == "dynamodb:UpdateItem"
    assert statements[0]["Resource"] == table_arn


def test_the_api_function_may_only_read_its_own_parameter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, template = synth(config(), tmp_path, monkeypatch)

    statements = [
        s
        for s in policy_statements(template)
        if any(action.startswith("ssm:") for action in as_list(s["Action"]))
    ]

    assert len(statements) == 1
    assert statements[0]["Action"] == "ssm:GetParameter"
    resource = json.dumps(statements[0]["Resource"])
    assert ":parameter/orders-api/local/rate-limits" in resource
    assert "*" not in resource


@pytest.mark.parametrize("seconds", [2, 60])
def test_the_function_learns_where_its_limits_are_and_how_long_to_cache_them(
    seconds: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, data, template = synth(
        config(rateLimitConfigCacheSeconds=seconds), tmp_path, monkeypatch
    )
    table_id = stack.get_logical_id(data.rate_limit_table.node.default_child)  # type: ignore[arg-type]

    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "orders_api.adapters.inbound.api_handler.handler",
            "Environment": {
                "Variables": Match.object_like(
                    {
                        "RATE_LIMIT_TABLE": {"Ref": table_id},
                        "RATE_LIMITS_PARAMETER": "/orders-api/local/rate-limits",
                        "RATE_LIMITS_MAX_AGE_SECONDS": str(seconds),
                    }
                )
            },
        },
    )
