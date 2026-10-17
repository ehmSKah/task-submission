"""The orders table, its list indexes and the API function's access to them (ADRs 0005 and
0012)."""

from pathlib import Path

import pytest

from orders_api.adapters.outbound.dynamodb_orders import BY_CREATED_INDEX, BY_OWNER_INDEX
from tests.unit.data_stack import ITEM_ACTIONS, as_list, config, synth


def test_the_api_function_may_query_exactly_the_two_list_indexes_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, data, template = synth(config(), tmp_path, monkeypatch)
    table_id = stack.get_logical_id(data.orders_table.node.default_child)  # type: ignore[arg-type]

    statements = [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if "dynamodb:Query" in as_list(statement["Action"])
    ]

    assert len(statements) == 1
    assert statements[0]["Action"] == "dynamodb:Query"
    assert statements[0]["Resource"] == [
        {"Fn::Join": ["", [{"Fn::GetAtt": [table_id, "Arn"]}, f"/index/{index}"]]}
        for index in (BY_OWNER_INDEX, BY_CREATED_INDEX)
    ]


def test_the_indexes_the_adapter_queries_are_the_ones_the_table_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, data, template = synth(config(), tmp_path, monkeypatch)
    table_id = stack.get_logical_id(data.orders_table.node.default_child)  # type: ignore[arg-type]

    indexes = template.to_json()["Resources"][table_id]["Properties"]["GlobalSecondaryIndexes"]

    assert {index["IndexName"] for index in indexes} == {BY_OWNER_INDEX, BY_CREATED_INDEX}


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
        if table_arn in as_list(statement["Resource"])
        for action in as_list(statement["Action"])
    }

    assert sorted(action for action in granted if action.endswith("Item")) == ITEM_ACTIONS
