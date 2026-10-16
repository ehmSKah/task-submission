import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from orders_api.adapters.inbound import authorizer_handler
from orders_api.auth.tokens import Identity, InvalidToken
from tests.builders import FakeLambdaContext

METHOD_ARN = "arn:aws:execute-api:us-east-1:000000000000:abc123/live/GET/api/v1/orders/42"


class StubValidator:
    def __init__(self, result: Identity | Exception) -> None:
        self.result = result
        self.tokens: list[str] = []

    def validate(self, token: str) -> Identity:
        self.tokens.append(token)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def event(authorization: str | None) -> dict[str, Any]:
    headers = {} if authorization is None else {"authorization": authorization}
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
    def install(result: Identity | Exception) -> StubValidator:
        stub = StubValidator(result)
        monkeypatch.setattr(authorizer_handler, "_validator", stub)
        return stub

    return install


def test_valid_token_allows_the_whole_api_and_passes_identity(use_validator: Any) -> None:
    stub = use_validator(Identity(subject="alice-sub", groups=("USER", "marketing")))

    result = authorize(event("Bearer token-1"))

    assert stub.tokens == ["token-1"]
    assert result["principalId"] == "alice-sub"
    statement = result["policyDocument"]["Statement"][0]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "execute-api:Invoke"
    assert statement["Resource"] == "arn:aws:execute-api:us-east-1:000000000000:abc123/live/*"
    assert result["context"] == {"sub": "alice-sub", "roles": "USER"}


@pytest.mark.parametrize("header", [None, "", "token-1", "Basic abc", "Bearer "])
def test_missing_or_malformed_header_is_unauthorized(use_validator: Any, header: str) -> None:
    use_validator(Identity(subject="x"))

    with pytest.raises(Exception, match=r"^Unauthorized$"):
        authorize(event(header))


def test_rejected_token_is_unauthorized(use_validator: Any) -> None:
    use_validator(InvalidToken("expired token"))

    with pytest.raises(Exception, match=r"^Unauthorized$"):
        authorize(event("Bearer token-1"))


def test_failure_to_reach_the_provider_is_not_disguised_as_unauthorized(
    use_validator: Any,
) -> None:
    use_validator(OSError("connection refused"))

    with pytest.raises(OSError):
        authorize(event("Bearer token-1"))


@pytest.fixture
def authorizer_logs() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    capture = logging.StreamHandler(stream)
    capture.setFormatter(authorizer_handler.logger.registered_formatter)
    authorizer_handler.logger.addHandler(capture)
    yield stream
    authorizer_handler.logger.removeHandler(capture)


def test_log_lines_follow_the_shared_logging_fields(
    use_validator: Any, authorizer_logs: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORDERS_API_ENV", "local")
    use_validator(InvalidToken("expired token"))

    with pytest.raises(Exception, match=r"^Unauthorized$"):
        authorize(event("Bearer secret-token-value"))

    line = json.loads(authorizer_logs.getvalue().splitlines()[-1])
    assert line["service"] == "orders-api"
    assert line["requestId"] == "gw-req-9"
    assert line["env"] == "local"
    assert line["reason"] == "expired token"
    assert "cold_start" in line
    assert "secret-token-value" not in authorizer_logs.getvalue()
