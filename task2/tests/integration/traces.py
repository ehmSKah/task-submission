"""Following one message's trace through the recorded spans and the stub's journal (ADR 0008)."""

from uuid import uuid4

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.integration.stack import eventually
from tests.support.spans import in_trace

TERMINAL_EVENTS = frozenset({"handover.completed", "order.duplicate", "dead_letter.written"})


def new_traceparent() -> tuple[str, str, str]:
    """A producer's traceparent, with its trace id and span id."""
    trace_id, span_id = uuid4().hex, uuid4().hex[:16]
    return f"00-{trace_id}-{span_id}-01", trace_id, span_id


def trace_spans(spans: InMemorySpanExporter, trace_id: str, topic: str) -> list[ReadableSpan]:
    """The trace's spans, once its message's span has ended: it ends last, after the handover
    is recorded."""
    return eventually(
        lambda: in_trace(spans.get_finished_spans(), trace_id),
        until=lambda found: any(span.name == f"process {topic}" for span in found),
        what=f"the span of the message in trace {trace_id}",
    )


def named(spans: list[ReadableSpan], name: str) -> list[ReadableSpan]:
    return [span for span in spans if span.name == name]


def parent_id(span: ReadableSpan) -> int | None:
    return span.parent.span_id if span.parent is not None else None


def span_id(span: ReadableSpan) -> int:
    assert span.context is not None
    return span.context.span_id
