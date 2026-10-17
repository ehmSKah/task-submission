"""The pure parts of the Schemathesis contract run (ADR 0011)."""

import re
from types import SimpleNamespace
from typing import Any

import pytest
from schemathesis.config import SchemathesisConfig

from orders_api.domain.order import OrderStatus
from orders_api_infra.openapi_extensions import load_spec
from tests.contract.harness import (
    CONFIG as CONTRACT_CONFIG,
)
from tests.contract.harness import (
    DELETABLE_ORDER_IDS,
    KNOWN_ORDER_IDS,
    StatusesSeen,
    bearer_token,
    config_with_known_orders,
    known_orders,
    success_statuses,
)

CONFIG = """
[generation]
max-examples = 7

[parameters]
"path.id" = { dictionary = "known_orders", probability = 0.5 }

[dictionaries.known_orders]
values = []

[dictionaries.deletable_orders]
values = []

[[operations]]
include-operation-id = "deleteOrder"
parameters = { "path.id" = { dictionary = "deletable_orders", probability = 0.5 } }
"""


def values(config: SchemathesisConfig, dictionary: str) -> list[object]:
    return [entry.value for entry in config.dictionaries[dictionary].entries]


def test_the_order_ids_fill_their_dictionaries() -> None:
    config = config_with_known_orders(CONFIG)

    assert config.projects.default.generation.max_examples == 7
    assert values(config, "known_orders") == list(KNOWN_ORDER_IDS)
    assert values(config, "deletable_orders") == list(DELETABLE_ORDER_IDS)


def test_a_config_without_the_dictionaries_is_a_mistake() -> None:
    with pytest.raises(KeyError):
        config_with_known_orders("[generation]\nmax-examples = 7\n")


def test_the_shipped_config_draws_ids_from_the_known_orders_and_deletes_from_their_own() -> None:
    config = config_with_known_orders(CONTRACT_CONFIG.read_text())

    assert config.projects.default.parameters["path.id"].dictionary == "known_orders"
    [deletes] = config.projects.default.operations.operations
    assert deletes.parameters["path.id"].dictionary == "deletable_orders"


def test_the_known_orders_cover_every_status_for_the_owner_and_one_other_order() -> None:
    orders = {order.id: order for order in known_orders(owner="alice-sub", other="bob-sub")}

    known = [orders[order_id] for order_id in KNOWN_ORDER_IDS]
    owned = [order for order in known if order.owner_id == "alice-sub"]
    assert {order.status for order in owned} == set(OrderStatus)
    assert [order.owner_id for order in known if order not in owned] == ["bob-sub"]


def test_each_owner_has_a_deletable_order() -> None:
    orders = {order.id: order for order in known_orders(owner="alice-sub", other="bob-sub")}

    assert [orders[order_id].owner_id for order_id in DELETABLE_ORDER_IDS] == [
        "alice-sub",
        "bob-sub",
    ]
    assert len(orders) == len(KNOWN_ORDER_IDS) + len(DELETABLE_ORDER_IDS)


def test_the_order_ids_are_valid_path_ids() -> None:
    pattern = load_spec()["paths"]["/api/v1/orders/{id}"]["parameters"][0]["schema"]["pattern"]

    assert all(re.fullmatch(pattern, id_) for id_ in KNOWN_ORDER_IDS + DELETABLE_ORDER_IDS)


def test_the_bearer_token_is_fetched_and_set_as_the_authorization_header() -> None:
    provider = bearer_token(lambda: "token-1")()
    case: Any = SimpleNamespace(headers={"If-Match": '"1"'})

    token = provider.get(case, None)  # type: ignore[arg-type]
    provider.set(case, token, None)  # type: ignore[arg-type]

    assert case.headers == {"If-Match": '"1"', "Authorization": "Bearer token-1"}


def test_the_bearer_token_replaces_a_generated_authorization_header() -> None:
    provider = bearer_token(lambda: "token-1")()
    case: Any = SimpleNamespace(headers=None)

    provider.set(case, "token-1", None)  # type: ignore[arg-type]

    assert case.headers == {"Authorization": "Bearer token-1"}


def test_every_operation_has_one_success_status() -> None:
    assert success_statuses(load_spec()) == {
        "GET /api/v1/health": 200,
        "POST /api/v1/orders": 201,
        "GET /api/v1/orders": 200,
        "GET /api/v1/orders/{id}": 200,
        "DELETE /api/v1/orders/{id}": 204,
        "PATCH /api/v1/orders/{id}/status": 200,
    }


def test_statuses_are_counted_per_caller_and_operation() -> None:
    seen = StatusesSeen()
    seen.record("admin", "GET /api/v1/orders", 200)
    seen.record("admin", "GET /api/v1/orders", 200)
    seen.record("admin", "GET /api/v1/orders", 400)
    seen.record("alice", "GET /api/v1/orders", 403)

    assert seen.lines() == [
        "admin  GET /api/v1/orders: 200 x2, 400 x1",
        "alice  GET /api/v1/orders: 403 x1",
    ]


def test_an_operation_whose_success_was_never_seen_is_reported() -> None:
    seen = StatusesSeen()
    seen.record("admin", "GET /api/v1/orders", 200)
    seen.record("alice", "DELETE /api/v1/orders/{id}", 403)

    missing = seen.never_succeeded(
        {"GET /api/v1/orders": 200, "DELETE /api/v1/orders/{id}": 204, "GET /api/v1/health": 200}
    )

    assert missing == {"DELETE /api/v1/orders/{id}": 204, "GET /api/v1/health": 200}
