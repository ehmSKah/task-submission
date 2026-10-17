"""`DELETE /api/v1/orders/{id}` through the API function (ADRs 0001 and 0004)."""

import io
import json

import pytest

from orders_api.adapters.inbound import api_handler
from orders_api.domain.order import OrderStatus
from tests.builders import an_order
from tests.unit.events import (
    ADMIN,
    delete_order_event,
    get_order_event,
    headers_of,
    invoke,
    problem_of,
)
from tests.unit.fakes import InMemoryOrderRepository, WireApi

ORDER = an_order()
UNKNOWN_ID = "0e6bb2c6-0000-4000-8000-000000000000"


class UntouchableRepository(InMemoryOrderRepository):
    def delete(self, order_id: str) -> bool:
        raise AssertionError("the repository must not be touched")


@pytest.fixture(autouse=True)
def repository(wire_api: WireApi) -> InMemoryOrderRepository:
    return wire_api([ORDER])


def test_the_admin_deletes_an_order_with_an_empty_204(repository: InMemoryOrderRepository) -> None:
    response = invoke(delete_order_event(ORDER.id))

    assert response["statusCode"] == 204
    assert not response["body"]
    assert "ETag" not in headers_of(response)
    assert headers_of(response)["X-Request-Id"] == "gw-req-1"
    assert repository.get(ORDER.id) is None


def test_a_deleted_order_is_a_404_to_read_and_to_delete_again() -> None:
    invoke(delete_order_event(ORDER.id))

    problem_of(invoke(get_order_event(ORDER.id, ADMIN)), 404, "not-found")
    problem_of(invoke(delete_order_event(ORDER.id)), 404, "not-found")


@pytest.mark.parametrize("status", list(OrderStatus))
def test_an_order_is_deleted_in_any_status(wire_api: WireApi, status: OrderStatus) -> None:
    wire_api([an_order(status=status)])

    assert invoke(delete_order_event(ORDER.id))["statusCode"] == 204


def test_an_unknown_order_is_a_404() -> None:
    body = problem_of(invoke(delete_order_event(UNKNOWN_ID)), 404, "not-found")

    assert body["instance"] == f"/api/v1/orders/{UNKNOWN_ID}"


def test_a_malformed_id_is_a_400_without_data_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_handler, "orders", lambda: UntouchableRepository())

    body = problem_of(invoke(delete_order_event("not-a-uuid")), 400, "validation-error")

    assert [error["parameter"] for error in body["errors"]] == ["id"]


def test_missing_authorizer_context_fails_closed() -> None:
    problem_of(invoke(delete_order_event(ORDER.id, authorizer=None)), 500, "internal-error")


def test_the_access_line_names_the_deleted_order(log_output: io.StringIO) -> None:
    invoke(delete_order_event(ORDER.id))

    lines = [json.loads(line) for line in log_output.getvalue().splitlines() if line]
    access = next(line for line in lines if line["message"] == "request")
    assert access["status"] == 204
    assert access["route"] == "DELETE /api/v1/orders/{id}"
    assert access["orderId"] == ORDER.id
    assert access["sub"] == "admin-sub"
