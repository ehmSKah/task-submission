"""Structured logs (ADR 0008): one JSON object per line, carrying the trace of the span that is
current, with sensitive fields redacted as a second line of defence."""

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from opentelemetry import trace

from order_consumer import logs


@pytest.fixture
def lines() -> Iterator[io.StringIO]:
    """Loggers cache their output on first use, so each test gets a logger of its own."""
    stream = io.StringIO()
    logs.configure("order-consumer", "DEBUG", stream=stream)
    yield stream
    structlog.reset_defaults()
    logging.basicConfig(force=True)


def logged(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


@pytest.mark.parametrize(
    "name",
    [
        "authorization",
        "Authorization",
        "token",
        "fulfilment_token",
        "password",
        "secret",
        "client_secret",
        "dsn",
        "database_dsn",
    ],
)
def test_a_field_whose_name_looks_sensitive_is_redacted(lines: io.StringIO, name: str) -> None:
    structlog.get_logger().info("handover.completed", **{name: "Bearer local-dev-token"})

    [entry] = logged(lines)
    assert entry[name] == "[redacted]"
    assert "local-dev-token" not in lines.getvalue()


def test_sensitive_fields_nested_in_a_field_are_redacted(lines: io.StringIO) -> None:
    structlog.get_logger().info(
        "kafka.error", request={"headers": {"Authorization": "Bearer local-dev-token"}}
    )

    assert "local-dev-token" not in lines.getvalue()
    [entry] = logged(lines)
    assert entry["request"] == {"headers": {"Authorization": "[redacted]"}}


def test_other_fields_are_kept(lines: io.StringIO) -> None:
    structlog.get_logger().info(
        "order.accepted", order_id="3f1c2a9e", attempt=2, error="503 Service Unavailable"
    )

    [entry] = logged(lines)
    assert (entry["event"], entry["order_id"], entry["attempt"], entry["error"]) == (
        "order.accepted",
        "3f1c2a9e",
        2,
        "503 Service Unavailable",
    )
    assert (entry["service"], entry["level"]) == ("order-consumer", "info")


def test_a_line_carries_the_trace_and_span_of_the_current_span(lines: io.StringIO) -> None:
    log = structlog.get_logger()
    with trace.get_tracer(__name__).start_as_current_span("process orders") as span:
        log.info("event.received")
        logging.getLogger("httpx").warning("from a library")
    log.info("consumer.stopped")

    received, library, stopped = logged(lines)
    expected = {
        "trace_id": trace.format_trace_id(span.get_span_context().trace_id),
        "span_id": trace.format_span_id(span.get_span_context().span_id),
    }
    assert {key: received[key] for key in expected} == expected
    assert {key: library[key] for key in expected} == expected
    assert "trace_id" not in stopped, "no span, no trace"
