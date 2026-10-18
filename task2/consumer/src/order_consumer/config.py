"""Settings from the environment, checked when the consumer starts (ADR 0005)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# Where the consumer image puts the event's JSON Schema (ADR 0005: one definition per contract).
EVENT_SCHEMA = Path("/app/schemas/order-placed.v1.schema.json")


class ConfigurationError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Settings:
    kafka_bootstrap_servers: str
    kafka_topic: str
    kafka_group_id: str
    database_url: str
    fulfilment_base_url: str
    fulfilment_token: str
    kafka_dead_letter_topic: str = "orders.dlt"
    event_schema: Path = EVENT_SCHEMA
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ConfigurationError(f"{name} is not set")
            return value

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
        )
        if not settings.event_schema.is_file():
            raise ConfigurationError(f"ORDER_EVENT_SCHEMA {settings.event_schema} is not a file")
        return settings
