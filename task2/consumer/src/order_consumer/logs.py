"""Structured logs: one JSON object per line on stdout, event name first (ADR 0008)."""

import logging
import socket
import sys

import structlog


def configure(service: str, level: str = "INFO") -> None:
    instance = socket.gethostname()

    def add_origin(
        _logger: object, _method: str, event_dict: structlog.types.EventDict
    ) -> structlog.types.EventDict:
        event_dict.setdefault("service", service)
        event_dict.setdefault("instance", instance)
        return event_dict

    before_rendering: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        add_origin,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
    ]
    structlog.configure(
        processors=[
            *before_rendering,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Library logs (librdkafka callbacks, httpx) go through the same JSON renderer.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
            foreign_pre_chain=before_rendering,
        )
    )
    logging.basicConfig(handlers=[handler], level=logging.WARNING, force=True)
