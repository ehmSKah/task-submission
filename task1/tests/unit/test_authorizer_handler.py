import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from orders_api.adapters.inbound import authorizer_handler
from orders_api.auth.oidc import SigningKeysUnavailable
from orders_api.auth.tokens import Identity, InvalidToken, Reason
from tests.builders import FakeLambdaContext

METHOD_ARN = "arn:aws:execute-api:us-east-1:000000000000:abc123/live/GET/api/v1/orders/42"
ALICE = Identity(subject="alice-sub", roles=("USER", "marketing"), client_id="orders-api-client")


class StubValidator:
    def __init__(self, result: Identity | Exception, fetch_keys: bool = False) -> None:
        self.result = result
        self.fetch_keys = fetch_keys
        self.headers: list[str | None] = []

    def validate(self, authorization: str | None) -> Identity:
        self.headers.append(authorization)
        if self.fetch_keys:
            authorizer_handler.report_key_event("jwks_refetch", kid="new-kid")
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def event(authorization: str | None, header_name: str = "authorization") -> dict[str, Any]:
    headers = {} if authorization is None else {header_name: authorization}
    return {
        "type": "REQUEST",
        "methodArn": METHOD_ARN,
        "headers": headers,
        "requestContext": {"requestId": "gw-req-9"},
    }


def authorize(request: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = authorizer_handler.handler(request, FakeLambdaContext())
    return result


@pytest.fixture
def use_validator(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(result: Identity | Exception, fetch_keys: bool = False) -> StubValidator:
        stub = StubValidator(result, fetch_keys)
        monkeypatch.setattr(authorizer_handler, "_validator", stub)
        return stub

    return install


@pytest.fixture
def authorizer_logs() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    capture = logging.StreamHandler(stream)
    capture.setFormatter(authorizer_handler.logger.registered_formatter)
    authorizer_handler.logger.addHandler(capture)
    yield stream
    authorizer_handler.logger.removeHandler(capture)


def log_lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_valid_token_allows_the_whole_api_and_passes_identity(use_validator: Any) -> None:
    stub = use_validator(ALICE)

    result = authorize(event("Bearer token-1"))

    assert stub.headers == ["Bearer token-1"]
    assert result["principalId"] == "alice-sub"
    statement = result["policyDocument"]["Statement"][0]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "execute-api:Invoke"
    assert statement["Resource"] == "arn:aws:execute-api:us-east-1:000000000000:abc123/live/*"
    assert result["context"] == {"sub": "alice-sub", "roles": "USER"}


@pytest.mark.parametrize("name", ["Authorization", "authorization", "AUTHORIZATION"])
def test_the_header_is_found_in_any_case(use_validator: Any, name: str) -> None:
    stub = use_validator(ALICE)

    authorize(event("Bearer token-1", header_name=name))

    assert stub.headers == ["Bearer token-1"]


def test_a_missing_header_goes_to_the_validator_as_none(use_validator: Any) -> None:
    stub = use_validator(InvalidToken(Reason.MALFORMED_HEADER))

    with pytest.raises(Exception, match=r"^Unauthorized$"):
        authorize(event(None))

    assert stub.headers == [None]


@pytest.mark.parametrize("reason", list(Reason))
def test_every_rejection_is_the_same_unauthorized(use_validator: Any, reason: Reason) -> None:
    use_validator(InvalidToken(reason))

    with pytest.raises(authorizer_handler.Unauthorized, match=r"^Unauthorized$"):
        authorize(event("Bearer token-1"))


def test_no_signing_keys_fails_the_authorizer_for_a_503(
    use_validator: Any, authorizer_logs: io.StringIO
) -> None:
    use_validator(SigningKeysUnavailable())

    with pytest.raises(SigningKeysUnavailable):
        authorize(event("Bearer token-1"))

    line = log_lines(authorizer_logs)[-1]
    assert line["message"] == "signing_keys_unavailable"
    assert line["level"] == "ERROR"


def test_a_rejection_logs_reason_kid_issuer_and_client_but_never_the_token(
    use_validator: Any, authorizer_logs: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORDERS_API_ENV", "local")
    use_validator(
        InvalidToken(
            Reason.CLIENT_NOT_ALLOWED,
            kid="us-east-1_pool",
            issuer="http://floci:4566/us-east-1_pool",
            client_id="orders-api-untrusted-client",
        )
    )

    with pytest.raises(Exception, match=r"^Unauthorized$"):
        authorize(event("Bearer secret-token-value"))

    line = log_lines(authorizer_logs)[-1]
    assert line["message"] == "token_rejected"
    assert line["service"] == "orders-api"
    assert line["requestId"] == "gw-req-9"
    assert line["env"] == "local"
    assert "cold_start" in line
    assert line["reason"] == "client_not_allowed"
    assert line["kid"] == "us-east-1_pool"
    assert line["iss"] == "http://floci:4566/us-east-1_pool"
    assert line["client_id"] == "orders-api-untrusted-client"
    assert line["keysFetched"] is False
    assert "secret-token-value" not in authorizer_logs.getvalue()


def test_an_acceptance_logs_subject_roles_and_client(
    use_validator: Any, authorizer_logs: io.StringIO
) -> None:
    use_validator(ALICE)

    authorize(event("Bearer secret-token-value"))

    line = log_lines(authorizer_logs)[-1]
    assert line["message"] == "token_accepted"
    assert line["sub"] == "alice-sub"
    assert line["roles"] == ["USER"]
    assert line["client_id"] == "orders-api-client"
    assert "secret-token-value" not in authorizer_logs.getvalue()


def test_key_events_are_logged_and_mark_the_request_as_having_fetched_keys(
    use_validator: Any, authorizer_logs: io.StringIO
) -> None:
    use_validator(ALICE, fetch_keys=True)

    authorize(event("Bearer token-1"))

    refetch, decision = log_lines(authorizer_logs)[-2:]
    assert refetch["message"] == "jwks_refetch"
    assert refetch["kid"] == "new-kid"
    assert refetch["requestId"] == "gw-req-9"
    assert decision["keysFetched"] is True

    use_validator(ALICE)
    authorize(event("Bearer token-2"))
    assert log_lines(authorizer_logs)[-1]["keysFetched"] is False


def test_a_failed_key_fetch_is_a_warning(authorizer_logs: io.StringIO) -> None:
    authorizer_handler.report_key_event("jwks_fetch_failed", error="timed out", staleKeyCount=1)

    line = log_lines(authorizer_logs)[-1]
    assert line["level"] == "WARNING"
    assert line["error"] == "timed out"


COGNITO_ENVIRONMENT = {
    "TOKEN_ISSUER": "http://floci:4566/us-east-1_pool",
    "TOKEN_TYPE_CLAIM": "token_use",
    "TOKEN_TYPE_VALUE": "access",
    "TOKEN_CLIENT_CLAIM": "client_id",
    "TOKEN_ALLOWED_CLIENTS": "orders-api-client, second-client",
    "TOKEN_ROLES_CLAIM": "cognito:groups",
}


def test_the_provider_settings_come_from_the_environment() -> None:
    config = authorizer_handler.config_from_environment(
        {
            **COGNITO_ENVIRONMENT,
            "TOKEN_JWKS_URI": "http://keys.example/jwks.json",
            "TOKEN_GROUP_ROLES": "staff=ORDER_MANAGER, customers=USER",
        }
    )

    assert config.issuer == "http://floci:4566/us-east-1_pool"
    assert config.jwks_uri == "http://keys.example/jwks.json"
    assert (config.token_type_claim, config.token_type_value) == ("token_use", "access")
    assert config.client_claim == "client_id"
    assert config.allowed_clients == frozenset({"orders-api-client", "second-client"})
    assert config.roles_claim == "cognito:groups"
    assert config.group_roles == {"staff": "ORDER_MANAGER", "customers": "USER"}


def test_optional_provider_settings_default_to_discovery_and_role_named_groups() -> None:
    config = authorizer_handler.config_from_environment(COGNITO_ENVIRONMENT)

    assert config.jwks_uri is None
    assert config.group_roles is None


def test_a_missing_provider_setting_fails_closed() -> None:
    incomplete = {k: v for k, v in COGNITO_ENVIRONMENT.items() if k != "TOKEN_ALLOWED_CLIENTS"}

    with pytest.raises(KeyError):
        authorizer_handler.config_from_environment(incomplete)
