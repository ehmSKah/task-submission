"""Settings from the environment, and the start-up checks that refuse a bad configuration
(ADR 0003, 0005, 0006, 0007)."""

import pytest
from structlog.testing import capture_logs

from order_consumer import main
from order_consumer.application.retries import RetryPolicy
from order_consumer.config import ConfigurationError, FulfilmentTimeouts, Settings
from tests.support.events import EVENT_SCHEMA

REQUIRED = {
    "KAFKA_BOOTSTRAP_SERVERS": "kafka:9092",
    "DATABASE_URL": "postgresql://order_consumer@postgres/orders",
    "FULFILMENT_BASE_URL": "http://fulfilment-stub:8081",
    "FULFILMENT_TOKEN": "local-dev-token",
    "ORDER_EVENT_SCHEMA": str(EVENT_SCHEMA),
}


def test_the_handover_settings_default_to_the_documented_values() -> None:
    settings = Settings.from_env(REQUIRED)

    assert settings.fulfilment_timeouts == FulfilmentTimeouts(
        connect=2.0, read=5.0, attempt_deadline=10.0
    )
    assert settings.retry_policy == RetryPolicy(
        max_attempts=5, backoff_base=1.0, backoff_cap=15.0, retry_after_cap=15.0
    )
    assert settings.kafka_max_poll_interval_ms == 300_000


def test_every_handover_setting_can_be_shortened() -> None:
    settings = Settings.from_env(
        REQUIRED
        | {
            "FULFILMENT_CONNECT_TIMEOUT_SECONDS": "0.5",
            "FULFILMENT_READ_TIMEOUT_SECONDS": "1",
            "FULFILMENT_ATTEMPT_DEADLINE_SECONDS": "2",
            "HANDOVER_MAX_ATTEMPTS": "3",
            "HANDOVER_BACKOFF_BASE_SECONDS": "0.1",
            "HANDOVER_BACKOFF_CAP_SECONDS": "0.4",
            "HANDOVER_RETRY_AFTER_CAP_SECONDS": "1.5",
            "KAFKA_MAX_POLL_INTERVAL_MS": "60000",
        }
    )

    assert settings.fulfilment_timeouts == FulfilmentTimeouts(
        connect=0.5, read=1.0, attempt_deadline=2.0
    )
    assert settings.retry_policy == RetryPolicy(
        max_attempts=3, backoff_base=0.1, backoff_cap=0.4, retry_after_cap=1.5
    )
    assert settings.kafka_max_poll_interval_ms == 60_000


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("HANDOVER_MAX_ATTEMPTS", "five"),
        ("HANDOVER_MAX_ATTEMPTS", "0"),
        ("FULFILMENT_READ_TIMEOUT_SECONDS", "-1"),
        ("HANDOVER_BACKOFF_CAP_SECONDS", "nan"),
        ("KAFKA_MAX_POLL_INTERVAL_MS", "1.5"),
    ],
)
def test_a_setting_that_is_not_a_positive_number_is_refused(name: str, value: str) -> None:
    with pytest.raises(ConfigurationError, match=name):
        Settings.from_env(REQUIRED | {name: value})


def test_a_retry_budget_above_half_the_poll_interval_is_refused() -> None:
    with pytest.raises(ConfigurationError, match=r"half of max\.poll\.interval\.ms"):
        Settings.from_env(REQUIRED | {"HANDOVER_MAX_ATTEMPTS": "10"})


def test_the_session_timeout_defaults_to_the_clients_45_seconds_and_can_be_shortened() -> None:
    """Shortened only by the reliability suite's zombie test (ADR 0007, 0009)."""
    assert Settings.from_env(REQUIRED).kafka_session_timeout_ms == 45_000
    shortened = Settings.from_env(REQUIRED | {"KAFKA_SESSION_TIMEOUT_MS": "6000"})
    assert shortened.kafka_session_timeout_ms == 6000


def test_a_session_timeout_above_the_poll_interval_is_refused() -> None:
    """The client refuses it too, but only once the consumer is created: as a crash."""
    env = REQUIRED | {"KAFKA_SESSION_TIMEOUT_MS": "300001"}

    with pytest.raises(ConfigurationError, match=r"KAFKA_SESSION_TIMEOUT_MS .* max\.poll"):
        Settings.from_env(env)


def test_the_consumer_refuses_to_start_with_exit_code_2(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KAFKA_MAX_POLL_INTERVAL_MS", "100000")
    monkeypatch.setattr(main.logs, "configure", lambda *_args: None)  # keep the captured logs

    with capture_logs() as logged:
        exit_code = main.main()

    assert exit_code == 2
    [refused] = logged
    assert refused["event"] == "consumer.configuration_invalid"
    assert "half of max.poll.interval.ms" in refused["reason"]


def test_without_an_otlp_endpoint_spans_are_not_exported() -> None:
    assert Settings.from_env(REQUIRED).otlp_endpoint is None
    assert Settings.from_env(REQUIRED | {"OTEL_EXPORTER_OTLP_ENDPOINT": " "}).otlp_endpoint is None


def test_the_otlp_endpoint_is_read_from_the_standard_variable() -> None:
    settings = Settings.from_env(REQUIRED | {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://jaeger:4318"})

    assert settings.otlp_endpoint == "http://jaeger:4318"


def test_an_otlp_endpoint_that_is_not_an_http_url_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="OTEL_EXPORTER_OTLP_ENDPOINT"):
        Settings.from_env(REQUIRED | {"OTEL_EXPORTER_OTLP_ENDPOINT": "jaeger:4318"})
