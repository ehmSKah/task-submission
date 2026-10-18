"""Structured logs: one JSON object per line on stdout, event name first (ADR 0008)."""

import logging
import re
import socket
import sys
from typing import Any, TextIO

import structlog
from opentelemetry import trace

# Defence in depth: log calls pass named fields, never whole objects, so these should never appear.
SENSITIVE = re.compile(r"authorization|token|password|secret|dsn", re.IGNORECASE)
REDACTED = "[redacted]"


def redact(_logger: object, _method: str, event_dict: structlog.types.EventDict) -> Any:
    """Replaces the value of every field whose name looks sensitive, nested ones included."""
    return _redacted(event_dict)


def _redacted(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if isinstance(key, str) and SENSITIVE.search(key) else _redacted(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redacted(item) for item in value]
    return value


def add_trace(
    _logger: object, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """The trace id and span id of the span that is current, if there is one."""
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict.setdefault("trace_id", trace.format_trace_id(span_context.trace_id))
        event_dict.setdefault("span_id", trace.format_span_id(span_context.span_id))
    return event_dict


def configure(service: str, level: str = "INFO", stream: TextIO | None = None) -> None:
    """`stream` is stdout unless a test reads the lines."""
    instance = socket.gethostname()
    output = stream or sys.stdout

    def add_origin(
        _logger: object, _method: str, event_dict: structlog.types.EventDict
    ) -> structlog.types.EventDict:
        event_dict.setdefault("service", service)
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
        processors=[
            *before_rendering,
            structlog.processors.dict_tracebacks,
            redact,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        # One write per line, so lines from several threads never interleave.
        logger_factory=structlog.WriteLoggerFactory(output),
        cache_logger_on_first_use=True,
    )

    # Library logs (librdkafka callbacks, httpx) go through the same JSON renderer.
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
    logging.basicConfig(handlers=[handler], level=logging.WARNING, force=True)
