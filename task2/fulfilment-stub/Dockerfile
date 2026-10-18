# The fulfilment stub. Built from task2/ (the workspace lock), installing only this member, with
# the OpenAPI contract it validates against.
FROM python:3.13-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY consumer/pyproject.toml consumer/
COPY fulfilment-stub/pyproject.toml fulfilment-stub/
RUN uv sync --frozen --no-dev --package fulfilment-stub --no-install-workspace
COPY fulfilment-stub/src fulfilment-stub/src
RUN uv sync --frozen --no-dev --package fulfilment-stub --no-editable

FROM python:3.13-slim-trixie
RUN useradd --system --uid 10001 stub
COPY --from=build /app/.venv /app/.venv
COPY openapi/fulfilment.yaml /app/openapi/fulfilment.yaml
# The stable HTTP conventions for the request spans (ADR 0008).
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 FULFILMENT_SPEC=/app/openapi/fulfilment.yaml \
    OTEL_SEMCONV_STABILITY_OPT_IN=http
USER stub
EXPOSE 8081
HEALTHCHECK --interval=2s --timeout=3s --retries=30 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8081/health')"]
CMD ["uvicorn", "--factory", "fulfilment_stub.main:app_from_env", "--host", "0.0.0.0", "--port", "8081", "--no-access-log"]
