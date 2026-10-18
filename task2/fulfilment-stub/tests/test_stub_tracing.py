"""The stub continues the consumer's trace and logs its trace id, and neither its logs nor its
spans carry the bearer token (ADR 0008)."""

import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fulfilment_stub.app import create_app
from fulfilment_stub.main import configure_logging

SPEC = Path(__file__).parents[2] / "openapi" / "fulfilment.yaml"
TOKEN = "a-token-that-must-not-leak"
ORDER_ID = "3f1c2a9e-8b4d-4c1e-9f2a-6d7e8f9a0b1c"
CUSTOMER_REFERENCE = "a9e2b7c4-1d3f-4e5a-8b6c-7d8e9f0a1b2c"
TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
TRACEPARENT = f"00-{TRACE_ID}-b7ad6b7169203331-01"


def request_body(**overrides: Any) -> dict[str, Any]:
    return {
        "externalReference": ORDER_ID,
        "customerReference": CUSTOMER_REFERENCE,
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
    } | overrides


def headers(token: str = TOKEN) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": ORDER_ID,
        "traceparent": TRACEPARENT,
    }


@pytest.fixture
def spans() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def lines() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging(stream)
    yield stream
    structlog.reset_defaults()


@pytest.fixture
def client(spans: InMemorySpanExporter) -> TestClient:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    return TestClient(create_app(spec_path=SPEC, token=TOKEN, tracer_provider=provider))


def logged(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def text_of(spans: tuple[ReadableSpan, ...]) -> str:
    return json.dumps(
        [
            {
                "name": span.name,
                "attributes": dict(span.attributes or {}),
                "events": [(event.name, dict(event.attributes or {})) for event in span.events],
                "status": span.status.description,
            }
            for span in spans
        ]
    )


def test_a_handover_continues_the_consumers_trace(
    client: TestClient, spans: InMemorySpanExporter
) -> None:
    client.post("/fulfilment-orders", json=request_body(), headers=headers())

    [span] = spans.get_finished_spans()
    assert span.kind is trace.SpanKind.SERVER
    assert trace.format_trace_id(span.context.trace_id) == TRACE_ID
    assert span.parent is not None and span.parent.span_id == 0xB7AD6B7169203331
    assert (span.attributes or {})["http.response.status_code"] == 201


def test_health_checks_and_the_control_side_are_not_traced(
    client: TestClient, spans: InMemorySpanExporter
) -> None:
    client.get("/health")
    client.get("/_control/requests")
    client.delete("/_control")

    assert spans.get_finished_spans() == ()


def test_the_journal_shows_the_traceparent_each_request_carried(client: TestClient) -> None:
    client.post("/fulfilment-orders", json=request_body(), headers=headers())

    [entry] = client.get("/_control/requests").json()
    assert entry["traceparent"] == TRACEPARENT


def test_the_stubs_log_lines_carry_the_trace_id(client: TestClient, lines: io.StringIO) -> None:
    client.post("/fulfilment-orders", json=request_body(), headers=headers())
    client.post("/fulfilment-orders", json=request_body(total={}), headers=headers())

    created, invalid = (
        entry
        for entry in logged(lines)
        if entry["event"] in ("fulfilment_order.created", "request.invalid")
    )
    assert created["trace_id"] == TRACE_ID
    assert invalid["trace_id"] == TRACE_ID


def test_neither_the_token_nor_the_customer_reaches_the_logs_or_spans(
    client: TestClient, spans: InMemorySpanExporter, lines: io.StringIO
) -> None:
    client.post("/fulfilment-orders", json=request_body(), headers=headers())
    client.post("/fulfilment-orders", json=request_body(), headers=headers("wrong"))
    client.post("/fulfilment-orders", json=request_body(total={}), headers=headers())
    logging.getLogger("uvicorn.error").warning("a library line")

    for text in (lines.getvalue(), text_of(spans.get_finished_spans())):
        assert TOKEN not in text
        assert "Bearer" not in text
        assert CUSTOMER_REFERENCE not in text
