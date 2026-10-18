"""Spans recorded in memory, so tests see every span without Jaeger (ADR 0008, 0009).

OpenTelemetry's global tracer provider can be set only once per process, so every suite shares
the one installed here.
"""

from functools import cache

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from order_consumer import tracing


@cache
def recorded_spans() -> InMemorySpanExporter:
    """The consumer's tracer provider, without an OTLP endpoint, plus an in-memory exporter."""
    provider = tracing.configure("order-consumer-test", otlp_endpoint=None)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    assert trace.get_tracer_provider() is provider, "another tracer provider was installed first"
    return exporter


def in_trace(spans: tuple[ReadableSpan, ...], trace_id: str) -> list[ReadableSpan]:
    """The trace's spans, in the order they ended."""
    return [
        span
        for span in spans
        if span.context is not None and trace.format_trace_id(span.context.trace_id) == trace_id
    ]


def all_attribute_values(spans: tuple[ReadableSpan, ...]) -> list[str]:
    """Everything a span can carry as text: names, attributes, events, links, status and the
    resource."""
    values: list[str] = []
    for span in spans:
        values.append(span.name)
        values.extend(f"{key}={value}" for key, value in (span.attributes or {}).items())
        values.extend(f"{key}={value}" for key, value in span.resource.attributes.items())
        for event in span.events:
            values.append(event.name)
            values.extend(f"{key}={value}" for key, value in (event.attributes or {}).items())
        for link in span.links:
            values.extend(f"{key}={value}" for key, value in (link.attributes or {}).items())
        if span.status.description:
            values.append(span.status.description)
    return values
