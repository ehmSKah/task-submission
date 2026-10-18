"""The fulfilment stub's HTTP interface (ADR 0003): the contract side and the control side."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
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


def test_a_contract_violation_names_the_fields_without_echoing_values(client: TestClient) -> None:
    secret = "VALUE-THAT-MUST-NOT-BE-ECHOED"
    response = client.post(
        "/fulfilment-orders",
        json=request_body(total={"amount": secret, "currency": "EUR"}),
        headers=headers(),
    )

    assert response.status_code == 400
    assert response.json()["errors"] == [{"pointer": "/total/amount", "rule": "pattern"}]
    assert secret not in response.text


def test_a_repeat_with_the_same_body_replays_the_original_response(client: TestClient) -> None:
    first = client.post("/fulfilment-orders", json=request_body(), headers=headers())

    repeat = client.post("/fulfilment-orders", json=request_body(), headers=headers())

    assert repeat.status_code == 201
    assert repeat.json() == first.json()
    assert len(client.get("/_control/fulfilment-orders").json()) == 1


def test_a_repeat_with_a_different_body_is_rejected_and_creates_nothing(
    client: TestClient,
) -> None:
    client.post("/fulfilment-orders", json=request_body(), headers=headers())

    repeat = client.post(
        "/fulfilment-orders",
        json=request_body(total={"amount": "40.00", "currency": "EUR"}),
        headers=headers(),
    )

    assert repeat.status_code == 422
    assert repeat.headers["content-type"] == "application/problem+json"
    assert len(client.get("/_control/fulfilment-orders").json()) == 1


def test_the_same_body_with_other_key_order_is_a_replay(client: TestClient) -> None:
    body = request_body()
    first = client.post("/fulfilment-orders", json=body, headers=headers())

    reordered = json.dumps(dict(reversed(body.items())), indent=2)
    repeat = client.post(
        "/fulfilment-orders",
        content=reordered,
        headers=headers() | {"Content-Type": "application/json"},
    )

    assert repeat.status_code == 201
    assert repeat.json() == first.json()


class HeldDelay:
    """Stands in for the stub's sleep: a delayed request waits until the test releases it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, _seconds: float) -> None:
        self.started.set()
        await self.release.wait()


def test_a_repeat_while_the_first_request_is_in_progress_gets_409() -> None:
    async def scenario() -> dict[str, Any]:
        delay = HeldDelay()
        app = create_app(spec_path=SPEC, token=TOKEN, sleep=delay)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://stub") as stub:
            await stub.post(
                "/_control/fault-rules",
                json={"externalReference": ORDER_ID, "times": 1, "delaySeconds": 5},
            )
            first = asyncio.create_task(
                stub.post("/fulfilment-orders", json=request_body(), headers=headers())
            )
            await delay.started.wait()
            repeat = await stub.post("/fulfilment-orders", json=request_body(), headers=headers())
            journal_meanwhile = (await stub.get("/_control/requests")).json()
            delay.release.set()
            created = await first
            later = await stub.post("/fulfilment-orders", json=request_body(), headers=headers())
            orders = (await stub.get("/_control/fulfilment-orders")).json()
        return {
            "repeat": repeat,
            "journal_meanwhile": journal_meanwhile,
            "created": created,
            "later": later,
            "orders": orders,
        }

    result = asyncio.run(scenario())

    assert result["repeat"].status_code == 409
    assert result["repeat"].headers["content-type"] == "application/problem+json"
    assert [entry["status"] for entry in result["journal_meanwhile"]] == [None, 409]
    assert result["created"].status_code == 201
    assert result["later"].status_code == 201
    assert result["later"].json() == result["created"].json()
    assert len(result["orders"]) == 1


OTHER_ORDER_ID = "5b6c7d8e-9f0a-4b1c-8d2e-3f4a5b6c7d8e"


def other_order() -> tuple[dict[str, Any], dict[str, str]]:
    return (
        request_body(externalReference=OTHER_ORDER_ID),
        headers() | {"Idempotency-Key": OTHER_ORDER_ID},
    )


@pytest.fixture
def delays() -> list[float]:
    return []


@pytest.fixture
def recording_client(delays: list[float]) -> TestClient:
    async def record(seconds: float) -> None:
        delays.append(seconds)

    return TestClient(create_app(spec_path=SPEC, token=TOKEN, sleep=record))


def test_a_delay_rule_applies_to_its_own_order_only(
    recording_client: TestClient, delays: list[float]
) -> None:
    recording_client.post(
        "/_control/fault-rules", json={"externalReference": ORDER_ID, "delaySeconds": 3}
    )
    body, other_headers = other_order()

    recording_client.post("/fulfilment-orders", json=body, headers=other_headers)
    assert delays == []
    recording_client.post("/fulfilment-orders", json=request_body(), headers=headers())
    assert delays == [3.0]


def test_a_rule_applies_to_the_given_number_of_requests(
    recording_client: TestClient, delays: list[float]
) -> None:
    recording_client.post(
        "/_control/fault-rules",
        json={"externalReference": ORDER_ID, "times": 2, "delaySeconds": 1},
    )

    for _ in range(3):
        recording_client.post("/fulfilment-orders", json=request_body(), headers=headers())

    assert delays == [1.0, 1.0]


def test_reset_forgets_keys_and_fault_rules(
    recording_client: TestClient, delays: list[float]
) -> None:
    recording_client.post("/fulfilment-orders", json=request_body(), headers=headers())
    recording_client.post(
        "/_control/fault-rules", json={"externalReference": ORDER_ID, "delaySeconds": 1}
    )

    recording_client.delete("/_control")
    other_body = request_body(total={"amount": "40.00", "currency": "EUR"})
    response = recording_client.post("/fulfilment-orders", json=other_body, headers=headers())

    assert response.status_code == 201
    assert delays == []


def test_the_request_journal_can_be_filtered_by_order(client: TestClient) -> None:
    client.post("/fulfilment-orders", json=request_body(), headers=headers())
    body, other_headers = other_order()
    client.post("/fulfilment-orders", json=body, headers=other_headers)

    journal = client.get("/_control/requests", params={"externalReference": OTHER_ORDER_ID})

    assert [entry["externalReference"] for entry in journal.json()] == [OTHER_ORDER_ID]
