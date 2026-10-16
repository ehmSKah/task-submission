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
        (CONFIG_DIR / "local.yaml").read_text().replace("target: floci", "target: aws")
    )

    with pytest.raises(ValidationError, match="only applies when target is floci"):
        load_config("awsish", tmp_path)


def test_everyone_is_user_is_off_unless_an_environment_turns_it_on(tmp_path: Path) -> None:
    assert load_config("local", CONFIG_DIR).everyone_is_user is False
    (tmp_path / "selfsignup.yaml").write_text(
        (CONFIG_DIR / "local.yaml").read_text() + "everyoneIsUser: true\n"
    )

    assert load_config("selfsignup", tmp_path).everyone_is_user is True
