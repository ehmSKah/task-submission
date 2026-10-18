from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.support.spans import recorded_spans


@pytest.fixture(scope="session", autouse=True)
def tracer_provider() -> None:
    """As the consumer installs it on start-up: without it, spans carry no trace id."""
    recorded_spans()


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """The spans ended during the test."""
    exporter = recorded_spans()
    exporter.clear()
    yield exporter
    exporter.clear()
