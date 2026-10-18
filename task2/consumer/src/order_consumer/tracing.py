"""Traces: W3C Trace Context as the correlation id, spans from the OpenTelemetry SDK (ADR 0008)."""

import socket
from collections.abc import Sequence

from opentelemetry import context, propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagators.textmap import Getter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from order_consumer.application.ports import MessageTrace, TraceSource

TRACEPARENT = "traceparent"
TRACESTATE = "tracestate"
# Longer is not a traceparent, whatever its version: it could be crafted to inject log lines.
MAX_TRACEPARENT_CHARACTERS = 128

_propagator = TraceContextTextMapPropagator()


def configure(service: str, otlp_endpoint: str | None) -> TracerProvider:
    """Installs the tracer provider. Without an OTLP endpoint, spans are created and discarded.

    Every trace is sampled, whatever the producer decided, so each order's trace can be followed;
    sampling at volume is a production concern (ADR 0008, 0010)."""
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": service, "service.instance.id": socket.gethostname()}
        ),
        sampler=ALWAYS_ON,
    )
    if otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    # Only W3C Trace Context goes out on handovers: no baggage.
    propagate.set_global_textmap(_propagator)
    return provider


def incoming_trace(
    headers: Sequence[tuple[str, bytes | None]],
) -> tuple[context.Context, TraceSource]:
    """The context to continue a record's trace in, with its tracestate, and where the trace comes
    from. A missing or invalid traceparent gives an empty context, so the message's span starts a
    new trace. Header names are matched whatever their case, as in HTTP."""
    values = [value for name, value in headers if name.lower() == TRACEPARENT]
    if not values:
        return context.Context(), TraceSource.NEW
    header = _ascii(values[0]) if len(values) == 1 else None
    if header is not None and len(header) <= MAX_TRACEPARENT_CHARACTERS:
        states = [_ascii(value) for name, value in headers if name.lower() == TRACESTATE]
        carrier = {TRACEPARENT: [header], TRACESTATE: [state for state in states if state]}
        continued = _propagator.extract(carrier, getter=_ListGetter())
        if trace.get_current_span(continued).get_span_context().is_valid:
            return continued, TraceSource.HEADER
    return context.Context(), TraceSource.REPLACED


def _ascii(value: bytes | None) -> str | None:
    try:
        return value.decode("ascii") if value is not None else None
    except UnicodeDecodeError:
        return None


class _ListGetter(Getter[dict[str, list[str]]]):
    """Each header's values, so repeated tracestate headers are combined as W3C asks."""

    def get(self, carrier: dict[str, list[str]], key: str) -> list[str] | None:
        return carrier.get(key) or None

    def keys(self, carrier: dict[str, list[str]]) -> list[str]:
        return list(carrier)


def current_trace(source: TraceSource) -> MessageTrace:
    """The trace of the span that is current: the message's own, when called as it starts."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        raise RuntimeError("no tracer provider: tracing.configure must run first")
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    return MessageTrace(
        trace_id=trace.format_trace_id(span_context.trace_id),
        traceparent=carrier[TRACEPARENT],
        source=source,
    )
