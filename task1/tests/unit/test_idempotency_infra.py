"""The idempotency table and the API function's access to it (ADRs 0005 and 0012)."""

from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from orders_api_infra import api as api_module
from orders_api_infra.api import Api
from orders_api_infra.config import EnvConfig, load_config
from orders_api_infra.data import Data

ITEM_ACTIONS = [
    "dynamodb:DeleteItem",
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
]


def config(**overrides: Any) -> EnvConfig:
    return EnvConfig.model_validate({**load_config("local").model_dump(by_alias=True), **overrides})


def synth(
    env_config: EnvConfig, bundle_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[cdk.Stack, Data, Template]:
    monkeypatch.setattr(api_module, "BUNDLE_DIR", bundle_dir)
    stack = cdk.Stack(cdk.App(), "IdempotencyUnderTest")
    data = Data(stack, "Data", config=env_config)
    Api(
        stack,
        "Api",
        config=env_config,
        authorizer_environment={},
        orders_table=data.orders_table,
        idempotency_table=data.idempotency_table,
    )
    return stack, data, Template.from_stack(stack)


@pytest.mark.parametrize("removal_policy", ["destroy", "retain"])
def test_the_idempotency_table_expires_records_and_is_always_disposable(
    removal_policy: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, data, template = synth(config(removalPolicy=removal_policy), tmp_path, monkeypatch)

    table_id = stack.get_logical_id(data.idempotency_table.node.default_child)  # type: ignore[arg-type]
    table = template.to_json()["Resources"][table_id]
    assert table["DeletionPolicy"] == "Delete"
    assert table["Properties"]["KeySchema"] == [{"AttributeName": "id", "KeyType": "HASH"}]
    assert table["Properties"]["BillingMode"] == "PAY_PER_REQUEST"
    assert table["Properties"]["TimeToLiveSpecification"] == {
        "AttributeName": "expiration",
        "Enabled": True,
    }
    assert table["Properties"]["PointInTimeRecoverySpecification"] == {
        "PointInTimeRecoveryEnabled": False
    }
    assert "StreamSpecification" not in table["Properties"]
    template.has_output("IdempotencyTableName", {})


def test_the_api_function_gets_exactly_the_four_item_actions_on_the_idempotency_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, data, template = synth(config(), tmp_path, monkeypatch)
    table_id = stack.get_logical_id(data.idempotency_table.node.default_child)  # type: ignore[arg-type]
    table_arn = {"Fn::GetAtt": [table_id, "Arn"]}

    statements = [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if table_arn in _as_list(statement["Resource"])
    ]

    assert len(statements) == 1
    assert sorted(_as_list(statements[0]["Action"])) == ITEM_ACTIONS
    assert statements[0]["Resource"] == table_arn
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "orders_api.adapters.inbound.api_handler.handler",
            "Environment": {
                "Variables": Match.object_like({"IDEMPOTENCY_TABLE": {"Ref": table_id}})
            },
        },
    )


def test_the_api_function_gets_exactly_the_four_item_actions_on_the_orders_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Get, create, change status and delete (ADRs 0005 and 0012); queries are not item actions."""
    stack, data, template = synth(config(), tmp_path, monkeypatch)
    table_id = stack.get_logical_id(data.orders_table.node.default_child)  # type: ignore[arg-type]
    table_arn = {"Fn::GetAtt": [table_id, "Arn"]}

    granted = {
        action
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if table_arn in _as_list(statement["Resource"])
        for action in _as_list(statement["Action"])
    }

    assert sorted(action for action in granted if action.endswith("Item")) == ITEM_ACTIONS


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]
