"""Following one message's trace through the recorded spans and the stub's journal (ADR 0008)."""

from dataclasses import dataclass
from uuid import UUID, uuid4

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.integration.stack import eventually, publish_records
from tests.support.events import order_placed_event
from tests.support.spans import in_trace

TERMINAL_EVENTS = frozenset({"handover.completed", "order.duplicate", "dead_letter.written"})


@dataclass(frozen=True)
class ProducerTrace:
    """The trace a producer published an event in."""

    trace_id: str
    span_id: str

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-01"


def publish_traced(topic: str, order_id: UUID, value: bytes | None = None) -> ProducerTrace:
    """Publishes the order's event, or `value`, with a traceparent of a new producer trace."""
    producer = ProducerTrace(trace_id=uuid4().hex, span_id=uuid4().hex[:16])
    event = order_placed_event(order_id) if value is None else value
    publish_records(
        topic, [(str(order_id).encode(), event, [("traceparent", producer.traceparent.encode())])]
    )
    return producer


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
