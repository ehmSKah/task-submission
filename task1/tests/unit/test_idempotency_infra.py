"""The idempotency table and the API function's access to it (ADRs 0005 and 0012)."""

from pathlib import Path

import pytest
from aws_cdk.assertions import Match

from tests.unit.data_stack import ITEM_ACTIONS, as_list, config, synth


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
        if table_arn in as_list(statement["Resource"])
    ]

    assert len(statements) == 1
    assert sorted(as_list(statements[0]["Action"])) == ITEM_ACTIONS
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
