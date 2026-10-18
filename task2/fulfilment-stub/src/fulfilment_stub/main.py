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
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
    )
    structlog.contextvars.bind_contextvars(service="fulfilment-stub", instance=socket.gethostname())


def app_from_env() -> FastAPI:
    configure_logging()
    return create_app(
        spec_path=Path(os.environ.get("FULFILMENT_SPEC", "/app/openapi/fulfilment.yaml")),
        token=os.environ["FULFILMENT_TOKEN"],
    )
