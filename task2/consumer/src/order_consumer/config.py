"""Settings from the environment, checked when the consumer starts (ADR 0005)."""

import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from order_consumer.application.retries import RetryBudgetExceeded, RetryPolicy
from order_consumer.health import HEALTH_FILE

# Where the consumer image puts the event's JSON Schema (ADR 0005: one definition per contract).
EVENT_SCHEMA = Path("/app/schemas/order-placed.v1.schema.json")


# librdkafka's default: a consumer that doesn't poll for this long loses its partitions.
MAX_POLL_INTERVAL_MS = 300_000
# librdkafka's default: a member silent for this long is evicted from the group (ADR 0007).
SESSION_TIMEOUT_MS = 45_000
# A paused instance tries the message in hand again this often (ADR 0006).
PAUSE_PROBE_INTERVAL_SECONDS = 30.0


class ConfigurationError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class FulfilmentTimeouts:
    """One handover attempt (ADR 0003)."""

    connect: float = 2.0
    read: float = 5.0  # also bounds writing the request
    attempt_deadline: float = 10.0  # the whole attempt, however the time is spent


@dataclass(frozen=True, slots=True)
class Settings:
    kafka_bootstrap_servers: str
    kafka_topic: str
    kafka_dead_letter_topic: str
    kafka_group_id: str
    database_url: str
    fulfilment_base_url: str
    fulfilment_token: str
    event_schema: Path = EVENT_SCHEMA
    log_level: str = "INFO"
    fulfilment_timeouts: FulfilmentTimeouts = field(default_factory=FulfilmentTimeouts)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    kafka_max_poll_interval_ms: int = MAX_POLL_INTERVAL_MS
    kafka_session_timeout_ms: int = SESSION_TIMEOUT_MS
    otlp_endpoint: str | None = None  # where spans are exported; without one they are discarded
    pause_probe_interval_seconds: float = PAUSE_PROBE_INTERVAL_SECONDS
    # Off by default: a paused instance keeps its partitions, right for shared causes (ADR 0006).
    pause_exit_after_minutes: float | None = None
    health_file: Path = HEALTH_FILE

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ConfigurationError(f"{name} is not set")
            return value

        def positive[N: (int, float), D: (int, float, None)](
            name: str, parse: Callable[[str], N], default: D
        ) -> N | D:
            raw = env.get(name, "").strip()
            if not raw:
                return default
            try:
                value = parse(raw)
            except ValueError:
                raise ConfigurationError(f"{name} is not a number: {raw[:64]!r}") from None
            if not (math.isfinite(value) and value > 0):
                raise ConfigurationError(f"{name} must be positive: {raw[:64]!r}")
            return value

        def http_url_or_none(name: str) -> str | None:
            value = env.get(name, "").strip()
            if value and not value.startswith(("http://", "https://")):
                raise ConfigurationError(f"{name} is not an http or https URL: {value[:64]!r}")
            return value or None

        timeouts, policy = FulfilmentTimeouts(), RetryPolicy()

        settings = cls(
            kafka_bootstrap_servers=required("KAFKA_BOOTSTRAP_SERVERS"),
            kafka_topic=env.get("KAFKA_TOPIC", "orders"),
            kafka_group_id=env.get("KAFKA_GROUP_ID", "order-consumer"),
            database_url=required("DATABASE_URL"),
            fulfilment_base_url=required("FULFILMENT_BASE_URL"),
            fulfilment_token=required("FULFILMENT_TOKEN"),
            kafka_dead_letter_topic=env.get("KAFKA_DEAD_LETTER_TOPIC", "orders.dlt"),
            event_schema=Path(env.get("ORDER_EVENT_SCHEMA", str(EVENT_SCHEMA))),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            fulfilment_timeouts=FulfilmentTimeouts(
                connect=positive("FULFILMENT_CONNECT_TIMEOUT_SECONDS", float, timeouts.connect),
                read=positive("FULFILMENT_READ_TIMEOUT_SECONDS", float, timeouts.read),
                attempt_deadline=positive(
                    "FULFILMENT_ATTEMPT_DEADLINE_SECONDS", float, timeouts.attempt_deadline
                ),
            ),
            retry_policy=RetryPolicy(
                max_attempts=positive("HANDOVER_MAX_ATTEMPTS", int, policy.max_attempts),
                backoff_base=positive("HANDOVER_BACKOFF_BASE_SECONDS", float, policy.backoff_base),
                backoff_cap=positive("HANDOVER_BACKOFF_CAP_SECONDS", float, policy.backoff_cap),
                retry_after_cap=positive(
                    "HANDOVER_RETRY_AFTER_CAP_SECONDS", float, policy.retry_after_cap
                ),
            ),
            kafka_max_poll_interval_ms=positive(
                "KAFKA_MAX_POLL_INTERVAL_MS", int, MAX_POLL_INTERVAL_MS
            ),
            kafka_session_timeout_ms=positive("KAFKA_SESSION_TIMEOUT_MS", int, SESSION_TIMEOUT_MS),
            otlp_endpoint=http_url_or_none("OTEL_EXPORTER_OTLP_ENDPOINT"),
            pause_probe_interval_seconds=positive(
                "PAUSE_PROBE_INTERVAL_SECONDS", float, PAUSE_PROBE_INTERVAL_SECONDS
            ),
            pause_exit_after_minutes=positive("PAUSE_EXIT_AFTER_MINUTES", float, None),
            health_file=Path(env.get("HEALTH_FILE", str(HEALTH_FILE))),
        )
        if settings.kafka_session_timeout_ms > settings.kafka_max_poll_interval_ms:
            raise ConfigurationError(
                f"KAFKA_SESSION_TIMEOUT_MS ({settings.kafka_session_timeout_ms}) exceeds "
                f"max.poll.interval.ms ({settings.kafka_max_poll_interval_ms})"
            )
        if not settings.event_schema.is_file():
            raise ConfigurationError(f"ORDER_EVENT_SCHEMA {settings.event_schema} is not a file")
        try:
            settings.retry_policy.check_budget(
                settings.fulfilment_timeouts.attempt_deadline, settings.kafka_max_poll_interval_ms
            )
        except RetryBudgetExceeded as exceeded:
            raise ConfigurationError(str(exceeded)) from None
        return settings
