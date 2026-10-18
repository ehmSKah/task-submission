# The order consumer. Built from task2/ (the workspace lock), installing only this member, with
# the event's JSON Schema it validates against.
# glibc base: confluent-kafka ships manylinux wheels only.
FROM python:3.13-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY consumer/pyproject.toml consumer/
COPY fulfilment-stub/pyproject.toml fulfilment-stub/
RUN uv sync --frozen --no-dev --package order-consumer --no-install-workspace
COPY consumer/src consumer/src
RUN uv sync --frozen --no-dev --package order-consumer --no-editable

FROM python:3.13-slim-trixie
RUN useradd --system --uid 10001 consumer
COPY --from=build /app/.venv /app/.venv
COPY schemas/order-placed.v1.schema.json /app/schemas/order-placed.v1.schema.json
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 \
    ORDER_EVENT_SCHEMA=/app/schemas/order-placed.v1.schema.json
USER consumer
CMD ["python", "-m", "order_consumer"]
