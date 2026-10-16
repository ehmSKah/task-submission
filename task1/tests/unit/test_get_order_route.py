import io
import json
from typing import Any

import pytest

from orders_api.adapters.inbound import api_handler
from tests.builders import FakeLambdaContext, an_order, rest_event
from tests.unit.events import ALICE, get_order_event
from tests.unit.fakes import InMemoryOrderRepository
from tests.unit.test_api_handler import headers_of

ORDER = an_order()


class ExplodingRepository(InMemoryOrderRepository):
    def get(self, order_id: str) -> None:
        raise AssertionError("the repository must not be called")


@pytest.fixture(autouse=True)
def repository(monkeypatch: pytest.MonkeyPatch) -> InMemoryOrderRepository:
    fake = InMemoryOrderRepository([ORDER])
    monkeypatch.setattr(api_handler, "_orders", fake)
    return fake


def invoke(event: dict[str, Any]) -> dict[str, Any]:
    response: dict[str, Any] = api_handler.handler(event, FakeLambdaContext())
    return response


def test_returns_the_order_in_its_published_shape() -> None:
    response = invoke(get_order_event(ORDER.id, ALICE))

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "id": ORDER.id,
        "ownerId": "alice-sub",
        "status": "PENDING",
        "currency": "EUR",
        "items": [{"sku": "ABC-123", "quantity": 2, "unitPrice": 1999}],
        "totalAmount": 3998,
        "createdAt": "2026-10-17T09:30:00.123Z",
        "updatedAt": "2026-10-17T09:30:00.123Z",
        "version": 1,
    }


def test_etag_is_the_quoted_version() -> None:
    assert headers_of(invoke(get_order_event(ORDER.id, ALICE)))["ETag"] == '"1"'


def test_unknown_order_is_a_problem_404() -> None:
    response = invoke(get_order_event("0e6bb2c6-0000-4000-8000-000000000000", ALICE))

    assert response["statusCode"] == 404
    assert json.loads(response["body"])["type"] == "urn:problem:orders-api:not-found"


def test_malformed_id_is_a_400_naming_the_parameter_without_data_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_handler, "_orders", ExplodingRepository())

    response = invoke(get_order_event("not-a-uuid", ALICE))

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert body["type"] == "urn:problem:orders-api:validation-error"
    assert body["detail"] == "Invalid path parameter: id"
    assert [error["parameter"] for error in body["errors"]] == ["id"]


def test_missing_authorizer_context_fails_closed() -> None:
    response = invoke(get_order_event(ORDER.id, None))

    assert response["statusCode"] == 500
    assert json.loads(response["body"])["type"] == "urn:problem:orders-api:internal-error"


def test_caller_identity_does_not_leak_into_the_next_request_logs(
    log_output: io.StringIO,
) -> None:
    invoke(get_order_event(ORDER.id, ALICE))
    log_output.truncate(0)
    log_output.seek(0)

    invoke(rest_event("GET", "/api/v1/health"))

    lines = [json.loads(line) for line in log_output.getvalue().splitlines() if line]
    assert lines
    assert all("sub" not in line and "orderId" not in line for line in lines)
