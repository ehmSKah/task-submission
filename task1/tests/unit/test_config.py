from pathlib import Path

import pytest
from pydantic import ValidationError

from orders_api_infra.config import Architecture, EnvConfig, load_config
from orders_api_infra.paths import CONFIG_DIR


def test_local_config_targets_floci() -> None:
    config = load_config("local", CONFIG_DIR)

    assert config.name == "local"
    assert config.target == "floci"
    assert config.account == "000000000000"
    assert config.stage_name


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("aarch64", Architecture.ARM_64),
        ("arm64", Architecture.ARM_64),
        ("x86_64", Architecture.X86_64),
        ("AMD64", Architecture.X86_64),
    ],
)
def test_host_architecture_follows_the_machine(machine: str, expected: Architecture) -> None:
    config = load_config("local", CONFIG_DIR)

    assert config.resolved_architecture(machine=machine) is expected


def test_explicit_architecture_ignores_the_machine() -> None:
    config = EnvConfig.model_validate(
        {
            **load_config("local", CONFIG_DIR).model_dump(by_alias=True),
            "lambdaArchitecture": "arm64",
        }
    )

    assert config.resolved_architecture(machine="x86_64") is Architecture.ARM_64


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text(
        (CONFIG_DIR / "local.yaml").read_text() + "\ntypoKey: true\n"
    )

    with pytest.raises(ValidationError):
        load_config("broken", tmp_path)


def test_missing_environment_is_a_clear_error() -> None:
    with pytest.raises(FileNotFoundError, match="nope"):
        load_config("nope", CONFIG_DIR)


def test_local_is_disposable_and_named_after_the_environment() -> None:
    config = load_config("local", CONFIG_DIR)

    assert config.stack_name == "OrdersApi-local"
    assert config.retains_data is False


def test_log_retention_must_be_a_supported_period(tmp_path: Path) -> None:
    (tmp_path / "odd.yaml").write_text(
        (CONFIG_DIR / "local.yaml")
        .read_text()
        .replace("logRetentionDays: 7", "logRetentionDays: 8")
    )

    with pytest.raises(ValidationError):
        load_config("odd", tmp_path)


def test_floci_needs_the_issuer_base_url(tmp_path: Path) -> None:
    text = (CONFIG_DIR / "local.yaml").read_text()
    (tmp_path / "noissuer.yaml").write_text(
        "\n".join(line for line in text.splitlines() if "flociIssuerBaseUrl" not in line)
    )

    with pytest.raises(ValidationError, match="flociIssuerBaseUrl"):
        load_config("noissuer", tmp_path)


def test_floci_settings_are_rejected_for_aws_targets(tmp_path: Path) -> None:
    (tmp_path / "awsish.yaml").write_text(
        (CONFIG_DIR / "local.yaml")
        .read_text()
        .replace("target: floci", "target: aws")
        .replace('account: "000000000000"\n', "")
    )

    with pytest.raises(ValidationError, match="only applies when target is floci"):
        load_config("awsish", tmp_path)


def test_everyone_is_user_is_off_unless_an_environment_turns_it_on(tmp_path: Path) -> None:
    assert load_config("local", CONFIG_DIR).everyone_is_user is False
    (tmp_path / "selfsignup.yaml").write_text(
        (CONFIG_DIR / "local.yaml").read_text() + "everyoneIsUser: true\n"
    )

    assert load_config("selfsignup", tmp_path).everyone_is_user is True


def test_rate_limits_are_cached_briefly_locally_and_for_a_minute_by_default(
    tmp_path: Path,
) -> None:
    """Local suites switch limits at runtime and wait out the cache (ADRs 0006 and 0010)."""
    assert load_config("local", CONFIG_DIR).rate_limit_config_cache_seconds == 2
    (tmp_path / "prodlike.yaml").write_text(
        (CONFIG_DIR / "local.yaml").read_text().replace("rateLimitConfigCacheSeconds: 2\n", "")
    )

    assert load_config("prodlike", tmp_path).rate_limit_config_cache_seconds == 60


def test_each_environment_has_its_own_rate_limits_parameter() -> None:
    assert load_config("local", CONFIG_DIR).rate_limits_parameter == "/orders-api/local/rate-limits"


def test_floci_names_its_fixed_account(tmp_path: Path) -> None:
    text = (CONFIG_DIR / "local.yaml").read_text()
    (tmp_path / "noaccount.yaml").write_text(
        "\n".join(line for line in text.splitlines() if not line.startswith("account:"))
    )

    with pytest.raises(ValidationError, match="account is required when target is floci"):
        load_config("noaccount", tmp_path)


@pytest.mark.parametrize("env", ["aws-dev", "aws-prod"])
def test_aws_environments_take_the_account_from_the_callers_credentials(env: str) -> None:
    """No account id in the repository: CDK deploys to the caller's account (ADR 0010)."""
    config = load_config(env, CONFIG_DIR)

    assert config.target == "aws"
    assert config.account is None
    assert config.region


def test_an_aws_environment_may_not_name_an_account(tmp_path: Path) -> None:
    (tmp_path / "pinned.yaml").write_text(
        (CONFIG_DIR / "aws-dev.yaml").read_text() + 'account: "123456789012"\n'
    )

    with pytest.raises(ValidationError, match="account comes from the caller's credentials"):
        load_config("pinned", tmp_path)


@pytest.mark.parametrize("env", ["aws-dev", "aws-prod"])
def test_aws_environments_run_on_arm64_at_info_with_access_logs(env: str) -> None:
    config = load_config(env, CONFIG_DIR)

    assert config.resolved_architecture(machine="x86_64") is Architecture.ARM_64
    assert config.log_level == "INFO"
    assert config.access_logs is True


def test_aws_dev_is_disposable_and_set_up_for_the_test_suites() -> None:
    config = load_config("aws-dev", CONFIG_DIR)

    assert config.retains_data is False
    assert config.seed_users is True
    assert config.test_fixtures is True
    assert config.rate_limit_config_cache_seconds == 2
    assert config.log_retention_days == 14
    assert config.waf_test_rule_limit is not None


def test_aws_prod_keeps_its_data_and_has_nothing_for_tests() -> None:
    config = load_config("aws-prod", CONFIG_DIR)

    assert config.retains_data is True
    assert config.seed_users is False
    assert config.test_fixtures is False
    assert config.rate_limit_config_cache_seconds == 60
    assert config.log_retention_days == 90
    assert config.waf_rate_limit == 3000
    assert config.waf_test_rule_limit is None


def test_access_logs_are_off_unless_an_environment_turns_them_on() -> None:
    assert load_config("local", CONFIG_DIR).access_logs is False


def test_the_waf_limit_defaults_to_adr_0006s_and_respects_wafs_minimum(tmp_path: Path) -> None:
    assert load_config("local", CONFIG_DIR).waf_rate_limit == 3000
    (tmp_path / "tiny.yaml").write_text(
        (CONFIG_DIR / "local.yaml").read_text() + "wafRateLimit: 9\n"
    )

    with pytest.raises(ValidationError):
        load_config("tiny", tmp_path)


def test_the_waf_test_rule_exists_only_with_the_test_fixtures(tmp_path: Path) -> None:
    (tmp_path / "nofixtures.yaml").write_text(
        (CONFIG_DIR / "local.yaml").read_text().replace("testFixtures: true", "testFixtures: false")
    )

    with pytest.raises(ValidationError, match="wafTestRuleLimit needs testFixtures"):
        load_config("nofixtures", tmp_path)
