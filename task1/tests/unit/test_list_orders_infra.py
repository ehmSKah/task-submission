"""The list indexes and the API function's access to them (ADRs 0005 and 0012)."""

from pathlib import Path
from typing import Any

import pytest

from orders_api.adapters.outbound.dynamodb_orders import BY_CREATED_INDEX, BY_OWNER_INDEX
from tests.unit.test_idempotency_infra import config, synth


def test_the_api_function_may_query_exactly_the_two_list_indexes_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, data, template = synth(config(), tmp_path, monkeypatch)
    table_id = stack.get_logical_id(data.orders_table.node.default_child)  # type: ignore[arg-type]

    statements = [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if "dynamodb:Query" in _as_list(statement["Action"])
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


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]
