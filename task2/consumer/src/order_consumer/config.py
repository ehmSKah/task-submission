"""Settings from the environment, checked when the consumer starts (ADR 0005)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass


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
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ConfigurationError(f"{name} is not set")
            return value

        return cls(
            kafka_bootstrap_servers=required("KAFKA_BOOTSTRAP_SERVERS"),
            kafka_topic=env.get("KAFKA_TOPIC", "orders"),
            kafka_group_id=env.get("KAFKA_GROUP_ID", "order-consumer"),
            database_url=required("DATABASE_URL"),
            fulfilment_base_url=required("FULFILMENT_BASE_URL"),
            fulfilment_token=required("FULFILMENT_TOKEN"),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
        )
