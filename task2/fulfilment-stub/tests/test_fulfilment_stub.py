"""The fulfilment stub's HTTP interface (ADR 0003): the contract side and the control side."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fulfilment_stub.app import create_app

SPEC = Path(__file__).parents[2] / "openapi" / "fulfilment.yaml"
TOKEN = "test-token"
ORDER_ID = "3f1c2a9e-8b4d-4c1e-9f2a-6d7e8f9a0b1c"


def request_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "externalReference": ORDER_ID,
        "customerReference": "a9e2b7c4-1d3f-4e5a-8b6c-7d8e9f0a1b2c",
        "orderedAt": "2026-10-17T09:12:03Z",
        "lines": [
            {
                "sku": "KNE-ESC-0042",
                "quantity": 2,
                "unitPrice": {"amount": "19.99", "currency": "EUR"},
                "lineTotal": {"amount": "39.98", "currency": "EUR"},
            }
        ],
        "total": {"amount": "39.98", "currency": "EUR"},
    }
    body.update(overrides)
    return body


def headers(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": ORDER_ID}


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(spec_path=SPEC, token=TOKEN))


def test_a_valid_handover_creates_a_fulfilment_order(client: TestClient) -> None:
    response = client.post("/fulfilment-orders", json=request_body(), headers=headers())

    assert response.status_code == 201
    assert response.json()["externalReference"] == ORDER_ID
    assert response.json()["id"].startswith("ful-")


def test_the_journal_lists_created_fulfilment_orders(client: TestClient) -> None:
    created = client.post("/fulfilment-orders", json=request_body(), headers=headers()).json()

    journal = client.get("/_control/fulfilment-orders", params={"externalReference": ORDER_ID})

    assert journal.json() == [{"id": created["id"], "externalReference": ORDER_ID}]


def test_the_journal_records_every_request_with_its_outcome(client: TestClient) -> None:
    client.post("/fulfilment-orders", json=request_body(), headers=headers())

    [entry] = client.get("/_control/requests").json()

    assert entry["externalReference"] == ORDER_ID
    assert entry["idempotencyKey"] == ORDER_ID
    assert entry["status"] == 201


def test_a_wrong_token_is_rejected_and_creates_nothing(client: TestClient) -> None:
    response = client.post("/fulfilment-orders", json=request_body(), headers=headers("wrong"))

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert client.get("/_control/fulfilment-orders").json() == []


def test_a_request_breaking_the_contract_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/fulfilment-orders", json=request_body(total={"amount": 39.98}), headers=headers()
    )

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert client.get("/_control/fulfilment-orders").json() == []


def test_a_handover_without_idempotency_key_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/fulfilment-orders", json=request_body(), headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert response.status_code == 400


def test_reset_clears_the_journal(client: TestClient) -> None:
    client.post("/fulfilment-orders", json=request_body(), headers=headers())

    client.delete("/_control")

    assert client.get("/_control/requests").json() == []
    assert client.get("/_control/fulfilment-orders").json() == []
