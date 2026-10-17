"""Typed per-environment configuration, loaded from `infra/config/<env>.yaml` (ADR 0010)."""

import platform
import re
from enum import Enum
from pathlib import Path
from typing import Literal

import aws_cdk as cdk
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orders_api_infra.paths import CONFIG_DIR

# Where an environment runs: the Floci emulator, or real AWS.
Target = Literal["floci", "aws"]

# The CloudWatch retention periods this project uses.
LogRetentionDays = Literal[1, 3, 7, 14, 30, 90]

# An origin as a browser sends it in `Origin`: scheme, lower-case host, and a port only where it
# isn't the scheme's default. No wildcard, path or trailing slash, so it can be compared exactly
# (ADR 0014).
_ORIGIN = re.compile(r"(?P<scheme>https?)://[a-z0-9-]+(\.[a-z0-9-]+)*(:(?P<port>[0-9]{1,5}))?")
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _check_origin(origin: str) -> str:
    match = _ORIGIN.fullmatch(origin)
    port = match and match["port"]
    if not match or (port and not 0 < int(port) < 65536):
        raise ValueError(
            f"corsAllowedOrigins: {origin!r} is not an origin as browsers send it: "
            "http(s)://host[:port], lower case, no wildcard, path or trailing slash"
        )
    if port and int(port) == _DEFAULT_PORTS[match["scheme"]]:
        raise ValueError(
            f"corsAllowedOrigins: {origin!r} names the default port, which browsers leave out"
        )
    return origin


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
    target: Target
    # Floci's fixed account. AWS environments name none: CDK deploys to the account of the
    # caller's credentials, so no account id is kept in the repository (ADR 0010).
    account: str | None = None
    region: str
    stage_name: str = Field(alias="stageName")
    # `host` builds for the machine running the deploy: Floci runs Lambda containers on the
    # host's architecture, so mismatched native wheels would fail to import (ADR 0010).
    lambda_architecture: Literal["host", "arm64", "x86_64"] = Field(alias="lambdaArchitecture")
    log_level: Literal["DEBUG", "INFO", "WARNING"] = Field(alias="logLevel")
    log_retention_days: LogRetentionDays = Field(alias="logRetentionDays")
    removal_policy: Literal["destroy", "retain"] = Field(alias="removalPolicy")
    # Whether demo users may be seeded; never in production (ADR 0010).
    seed_users: bool = Field(alias="seedUsers")
    # Test-only Cognito resources: a foreign user pool, an untrusted app client and a
    # short-lived client for expiry tests. Never in production (ADRs 0010 and 0011).
    test_fixtures: bool = Field(default=False, alias="testFixtures")
    # The short-lived client's access-token lifetime. AWS's minimum is 5 minutes; Floci accepts
    # seconds when FLOCI_SERVICES_COGNITO_ENFORCE_TOKEN_VALIDITY_LIMITS is false.
    fixture_token_validity_seconds: int = Field(
        default=300, ge=1, alias="fixtureTokenValiditySeconds"
    )
    # ADR 0001's opt-in switch for self-signup: every authenticated person also holds USER.
    # Off by default, so a valid token without a recognised role is denied (403).
    everyone_is_user: bool = Field(default=False, alias="everyoneIsUser")
    # How long each Lambda instance caches the runtime rate limits: short where tests switch them,
    # a minute in production, so changes take effect within that time (ADR 0006).
    rate_limit_config_cache_seconds: int = Field(
        default=60, ge=1, le=300, alias="rateLimitConfigCacheSeconds"
    )
    # API Gateway access logs on the stage (ADR 0012). Off locally: Floci accepts the setting but
    # neither keeps nor writes it.
    access_logs: bool = Field(default=False, alias="accessLogs")
    # The browser origins allowed to call the API cross-origin (ADR 0014). Empty, the default,
    # turns CORS off: no `Access-Control-*` headers and no `OPTIONS` routes, as before.
    cors_allowed_origins: tuple[str, ...] = Field(default=(), alias="corsAllowedOrigins")
    # WAF's rate-based rule: requests per client IP in any 5 minutes before the edge answers
    # `429`. AWS WAF's minimum is 10 (ADR 0006).
    waf_rate_limit: int = Field(default=3000, ge=10, le=2_000_000_000, alias="wafRateLimit")
    # A second, test-only rate-based rule with a deliberately low limit that counts only requests
    # carrying the WAF test header, so the `aws_only` test can trip it without the other suites'
    # requests from the same address ever being blocked. Only with testFixtures (ADR 0011).
    waf_test_rule_limit: int | None = Field(
        default=None, ge=10, le=2_000_000_000, alias="wafTestRuleLimit"
    )
    # Floci only: the issuer host tokens carry (FLOCI_HOSTNAME), reachable from Lambda
    # containers. On AWS the issuer comes from the user pool itself.
    floci_issuer_base_url: str | None = Field(default=None, alias="flociIssuerBaseUrl")
    # Floci only: a pinned user pool id, so the issuer stays the same across deploys.
    floci_user_pool_id: str | None = Field(default=None, alias="flociUserPoolId")

    @property
    def stack_name(self) -> str:
        return f"OrdersApi-{self.name}"

    @property
    def rate_limits_parameter(self) -> str:
        """The SSM parameter holding this environment's rate limits (ADR 0006)."""
        return f"/orders-api/{self.name}/rate-limits"

    @property
    def retains_data(self) -> bool:
        """Production keeps data and protects the stack; dev environments are disposable."""
        return self.removal_policy == "retain"

    @property
    def data_removal_policy(self) -> "cdk.RemovalPolicy":
        return cdk.RemovalPolicy.RETAIN if self.retains_data else cdk.RemovalPolicy.DESTROY

    @field_validator("cors_allowed_origins")
    @classmethod
    def _origins_as_browsers_send_them(cls, origins: tuple[str, ...]) -> tuple[str, ...]:
        for origin in origins:
            _check_origin(origin)
            if origins.count(origin) > 1:
                raise ValueError(f"corsAllowedOrigins: {origin!r} is listed more than once")
        return origins

    @model_validator(mode="after")
    def _floci_settings_match_the_target(self) -> "EnvConfig":
        if self.target == "floci" and not self.account:
            raise ValueError("account is required when target is floci")
        if self.target == "aws" and self.account:
            raise ValueError(
                "account comes from the caller's credentials on AWS; remove it from the config"
            )
        floci_settings = {
            "flociIssuerBaseUrl": self.floci_issuer_base_url,
            "flociUserPoolId": self.floci_user_pool_id,
        }
        for key, value in floci_settings.items():
            if self.target == "floci" and not value:
                raise ValueError(f"{key} is required when target is floci")
            if self.target == "aws" and value:
                raise ValueError(f"{key} only applies when target is floci")
        return self

    @model_validator(mode="after")
    def _test_rule_only_with_fixtures(self) -> "EnvConfig":
        if self.waf_test_rule_limit is not None and not self.test_fixtures:
            raise ValueError("wafTestRuleLimit needs testFixtures: true (ADR 0011)")
        return self

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
