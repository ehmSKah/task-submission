"""Traces: W3C Trace Context as the correlation id, spans from the OpenTelemetry SDK (ADR 0008)."""

import socket
from collections.abc import Sequence

from opentelemetry import context, propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from order_consumer.application.ports import MessageTrace, TraceSource

TRACEPARENT = "traceparent"
TRACESTATE = "tracestate"
# Longer is not a traceparent, whatever its version: it could be crafted to inject log lines.
MAX_TRACEPARENT_CHARACTERS = 128

_propagator = TraceContextTextMapPropagator()


def configure(service: str, otlp_endpoint: str | None) -> TracerProvider:
    """Installs the tracer provider. Without an OTLP endpoint, spans are created and discarded."""
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": service, "service.instance.id": socket.gethostname()}
        )
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
    """The context to continue a record's trace in, and where the trace comes from. A missing or
    invalid traceparent gives an empty context, so the message's span starts a new trace."""
    values = [value for name, value in headers if name == TRACEPARENT]
    if not values:
        return context.Context(), TraceSource.NEW
    if len(values) == 1 and values[0] is not None:
        try:
            header = values[0].decode("ascii")
        except UnicodeDecodeError:
            header = None
        if header is not None and len(header) <= MAX_TRACEPARENT_CHARACTERS:
            continued = _propagator.extract({TRACEPARENT: header})
            if trace.get_current_span(continued).get_span_context().is_valid:
                return continued, TraceSource.HEADER
    return context.Context(), TraceSource.REPLACED


def current_trace(source: TraceSource) -> MessageTrace:
    """The trace of the span that is current: the message's own, when called as it starts."""
    span_context = trace.get_current_span().get_span_context()
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    return MessageTrace(
        trace_id=trace.format_trace_id(span_context.trace_id),
        traceparent=carrier[TRACEPARENT],
        source=source,
    )
