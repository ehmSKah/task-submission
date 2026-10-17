"""Synthesising the data and API constructs for infrastructure unit tests."""

from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from orders_api_infra import api as api_module
from orders_api_infra.api import Api
from orders_api_infra.config import EnvConfig, load_config
from orders_api_infra.data import Data

ITEM_ACTIONS = [  # every item-level action the API function is granted on a table
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
    stack = cdk.Stack(cdk.App(), "DataUnderTest")
    data = Data(stack, "Data", config=env_config)
    Api(
        stack,
        "Api",
        config=env_config,
        authorizer_environment={},
        orders_table=data.orders_table,
        idempotency_table=data.idempotency_table,
        rate_limit_table=data.rate_limit_table,
    )
    return stack, data, Template.from_stack(stack)


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]
