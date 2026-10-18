"""Runs the stub from the environment: `uvicorn --factory fulfilment_stub.main:app_from_env`."""

import logging
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any, TextIO

import structlog
from fastapi import FastAPI
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from fulfilment_stub.app import create_app

# Defence in depth, as in the consumer: the stub's log calls never pass a header or a body.
SENSITIVE = re.compile(r"authorization|token|password|secret|dsn", re.IGNORECASE)


def redact(_logger: object, _method: str, event_dict: structlog.types.EventDict) -> Any:
    def redacted(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "[redacted]"
                if isinstance(key, str) and SENSITIVE.search(key)
                else redacted(item)
                for key, item in value.items()
            }
        if isinstance(value, list | tuple):
            return [redacted(item) for item in value]
        return value

    return redacted(event_dict)


def add_trace(
    _logger: object, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """The trace id and span id of the request's span, if there is one."""
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict.setdefault("trace_id", trace.format_trace_id(span_context.trace_id))
        event_dict.setdefault("span_id", trace.format_span_id(span_context.span_id))
    return event_dict


def configure_logging(stream: TextIO | None = None) -> None:
    """The same log shape as the consumer (ADR 0008); the stub shares no code with it (ADR 0005).
    `stream` is stdout unless a test reads the lines."""
    instance = socket.gethostname()
    output = stream or sys.stdout

    def add_origin(
        _logger: object, _method: str, event_dict: structlog.types.EventDict
    ) -> structlog.types.EventDict:
        event_dict.setdefault("service", "fulfilment-stub")
        event_dict.setdefault("instance", instance)
        return event_dict

    before_rendering: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        add_origin,
        add_trace,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
    ]
    structlog.configure(
        processors=[*before_rendering, redact, structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.WriteLoggerFactory(output),
    )

    # Uvicorn installs its own handlers before it loads this factory; replace them so its lines
    # are JSON too.
    handler = logging.StreamHandler(output)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                redact,
                structlog.processors.JSONRenderer(),
            ],
            foreign_pre_chain=before_rendering,
        )
    )
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        library_logger = logging.getLogger(name)
        library_logger.handlers = [handler]
        library_logger.propagate = False


def configure_tracing(otlp_endpoint: str | None) -> TracerProvider:
    """Spans are exported over OTLP when an endpoint is configured, and discarded otherwise."""
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": "fulfilment-stub", "service.instance.id": socket.gethostname()}
        )
    )
    if otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    # The consumer sends W3C Trace Context only; baggage is not read.
    propagate.set_global_textmap(TraceContextTextMapPropagator())
    return provider


def app_from_env() -> FastAPI:
    configure_logging()
    return create_app(
        spec_path=Path(os.environ.get("FULFILMENT_SPEC", "/app/openapi/fulfilment.yaml")),
        token=os.environ["FULFILMENT_TOKEN"],
        tracer_provider=configure_tracing(
            os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip() or None
        ),
    )
