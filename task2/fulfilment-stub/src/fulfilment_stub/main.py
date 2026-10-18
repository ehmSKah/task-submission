"""Runs the stub from the environment: `uvicorn --factory fulfilment_stub.main:app_from_env`."""

import logging
import os
import socket
import sys
from pathlib import Path

import structlog
from fastapi import FastAPI

from fulfilment_stub.app import create_app


def configure_logging() -> None:
    """The same log shape as the consumer (ADR 0008); the stub shares no code with it (ADR 0005)."""
    instance = socket.gethostname()

    def add_origin(
        _logger: object, _method: str, event_dict: structlog.types.EventDict
    ) -> structlog.types.EventDict:
        event_dict.setdefault("service", "fulfilment-stub")
        event_dict.setdefault("instance", instance)
        return event_dict

    before_rendering: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        add_origin,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
    ]
    structlog.configure(
        processors=[*before_rendering, structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
    )

    # Uvicorn installs its own handlers before it loads this factory; replace them so its lines
    # are JSON too.
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
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        library_logger = logging.getLogger(name)
        library_logger.handlers = [handler]
        library_logger.propagate = False


def app_from_env() -> FastAPI:
    configure_logging()
    return create_app(
        spec_path=Path(os.environ.get("FULFILMENT_SPEC", "/app/openapi/fulfilment.yaml")),
        token=os.environ["FULFILMENT_TOKEN"],
    )
