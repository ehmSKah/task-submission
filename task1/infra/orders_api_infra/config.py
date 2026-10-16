"""Typed per-environment configuration, loaded from `infra/config/<env>.yaml` (ADR 0010)."""

import platform
from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from orders_api_infra.paths import CONFIG_DIR

# The CloudWatch retention periods this project uses.
LogRetentionDays = Literal[1, 3, 7, 14, 30, 90]


class Architecture(Enum):
    ARM_64 = "arm64"
    X86_64 = "x86_64"

    @property
    def manylinux_platform(self) -> str:
        """The `uv pip install --python-platform` value for wheels of this architecture."""
        return {"arm64": "aarch64-manylinux_2_28", "x86_64": "x86_64-manylinux_2_28"}[self.value]


class EnvConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    name: str
    target: Literal["floci", "aws"]
    account: str
    region: str
    stage_name: str = Field(alias="stageName")
    # `host` builds for the machine running the deploy: Floci runs Lambda containers on the
    # host's architecture, so mismatched native wheels would fail to import (ADR 0010).
    lambda_architecture: Literal["host", "arm64", "x86_64"] = Field(alias="lambdaArchitecture")
    log_level: Literal["DEBUG", "INFO", "WARNING"] = Field(alias="logLevel")
    log_retention_days: LogRetentionDays = Field(alias="logRetentionDays")
    removal_policy: Literal["destroy", "retain"] = Field(alias="removalPolicy")

    @property
    def stack_name(self) -> str:
        return f"OrdersApi-{self.name}"

    @property
    def retains_data(self) -> bool:
        """Production keeps data and protects the stack; dev environments are disposable."""
        return self.removal_policy == "retain"

    def resolved_architecture(self, machine: str | None = None) -> Architecture:
        if self.lambda_architecture != "host":
            return Architecture(self.lambda_architecture)
        machine = (machine or platform.machine()).lower()
        return Architecture.ARM_64 if machine in {"aarch64", "arm64"} else Architecture.X86_64


def load_config(env: str, config_dir: Path = CONFIG_DIR) -> EnvConfig:
    path = config_dir / f"{env}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"No environment config '{env}' at {path}")
    return EnvConfig.model_validate(yaml.safe_load(path.read_text()))
