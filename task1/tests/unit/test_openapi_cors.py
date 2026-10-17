"""CORS in the gateway definition: a preflight on every path, answered by API Gateway itself, and
the CORS headers on every Gateway Response, only where the environment allows origins
(ADR 0014). How Floci renders them is checked end to end in tests/integration/test_cors.py."""

import copy
from typing import Any

import pytest

from orders_api.adapters.inbound.http_headers import SECURITY_HEADERS
from orders_api_infra.openapi_extensions import Target, load_spec, with_aws_extensions

LAMBDA_URI = "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/fn/invocations"
AUTHORIZER_URI = "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/auth/invocations"
SWAGGER_UI = "http://localhost:8080"
APP = "https://app.example.com"
EXPOSED = "ETag, RateLimit-Limit, RateLimit-Remaining, RateLimit-Reset, Retry-After, X-Request-Id"


def extended(origins: tuple[str, ...] = (), target: Target = "aws") -> dict[str, Any]:
    return with_aws_extensions(
        load_spec(),
        api_integration_uri=LAMBDA_URI,
        authorizer_uri=AUTHORIZER_URI,
        target=target,
        cors_allowed_origins=origins,
    )


def preflights(definition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        path: path_item["options"]
        for path, path_item in definition["paths"].items()
        if "options" in path_item
    }


def preflight_template(operation: dict[str, Any]) -> str:
    template: str = operation["x-amazon-apigateway-integration"]["responses"]["default"][
        "responseTemplates"
    ]["application/json"]
    return template


def overridden(template: str, header: str) -> str | None:
    """The value the template sets for `header`, or `None` if it sets none."""
    prefix = f"#set($context.responseOverride.header['{header}'] = "
    lines = [line for line in template.splitlines() if line.startswith(prefix)]
    assert len(lines) <= 1, header
    return lines[0].removeprefix(prefix).removesuffix(")") if lines else None


def test_without_allowed_origins_the_definition_is_as_before() -> None:
    """aws-prod, and any environment without browser clients: no `OPTIONS`, no CORS header."""
    definition = extended(())

    assert preflights(definition) == {}
    assert "Access-Control" not in str(definition)
    assert "Vary" not in str(definition["x-amazon-apigateway-gateway-responses"])


def test_the_published_contract_lists_no_preflight() -> None:
    """CORS depends on the environment, the contract doesn't (ADR 0014)."""
    spec = load_spec()
    original = copy.deepcopy(spec)

    with_aws_extensions(
        spec,
        api_integration_uri=LAMBDA_URI,
        authorizer_uri=AUTHORIZER_URI,
        target="aws",
        cors_allowed_origins=(SWAGGER_UI,),
    )

    assert spec == original
    assert preflights(spec) == {}


def test_every_path_gets_a_preflight_that_needs_no_token_and_invokes_no_function() -> None:
    definition = extended((SWAGGER_UI,))

    assert preflights(definition).keys() == definition["paths"].keys()
    for path, operation in preflights(definition).items():
        assert operation["security"] == [], path
        integration = operation["x-amazon-apigateway-integration"]
        assert integration["type"] == "mock", path
        assert integration["requestTemplates"] == {"application/json": '{"statusCode": 204}'}
        assert integration["responses"]["default"]["statusCode"] == "204"
        assert "x-amazon-apigateway-request-validator" not in operation


@pytest.mark.parametrize(
    ("path", "methods"),
    [
        ("/api/v1/health", "GET"),
        ("/api/v1/orders", "GET, POST"),
        ("/api/v1/orders/{id}", "DELETE, GET"),
        ("/api/v1/orders/{id}/status", "PATCH"),
    ],
)
def test_a_preflight_allows_the_paths_own_methods_and_the_apis_request_headers(
    path: str, methods: str
) -> None:
    template = preflight_template(preflights(extended((SWAGGER_UI,)))[path])

    assert overridden(template, "Access-Control-Allow-Methods") == f"'{methods}'"
    assert overridden(template, "Access-Control-Allow-Headers") == (
        "'Authorization, Content-Type, Idempotency-Key, If-Match'"
    )
    assert overridden(template, "Access-Control-Max-Age") == "'7200'"
    assert overridden(template, "Access-Control-Allow-Credentials") is None


@pytest.mark.parametrize("origins", [(SWAGGER_UI,), (SWAGGER_UI, APP)])
def test_a_preflight_echoes_the_origin_only_when_it_is_allowed(origins: tuple[str, ...]) -> None:
    """Every `Access-Control-*` header is set inside the one condition on the allowed origins,
    so any other origin gets none of them; the request id is set for every preflight."""
    template = preflight_template(preflights(extended(origins))["/api/v1/orders"])
    lines = template.splitlines()
    condition = " || ".join(f"$origin == '{origin}'" for origin in origins)
    start = lines.index(f"#if({condition})")
    end = lines.index("#end", start)

    inside = "\n".join(lines[start + 1 : end])
    outside = "\n".join(lines[:start] + lines[end + 1 :])
    assert overridden(inside, "Access-Control-Allow-Origin") == "$origin"
    assert "Access-Control-" not in outside
    assert overridden(outside, "X-Request-Id") == "$context.requestId"


def test_a_preflight_finds_the_origin_header_in_any_casing() -> None:
    """HTTP/2 lower-cases header names; Floci's `$input.params('Origin')` matches exactly."""
    template = preflight_template(preflights(extended((SWAGGER_UI,)))["/api/v1/orders"])

    assert "$name.equalsIgnoreCase('Origin')" in template
    assert "$input.params('Origin')" not in template


def test_a_preflight_carries_the_security_headers_and_varies_by_origin() -> None:
    operation = preflights(extended((SWAGGER_UI,)))["/api/v1/orders"]
    response = operation["x-amazon-apigateway-integration"]["responses"]["default"]
    params = response["responseParameters"]

    assert params["method.response.header.Vary"] == "'Origin'"
    for name, value in SECURITY_HEADERS.items():
        assert params[f"method.response.header.{name}"] == f"'{value}'"
    # API Gateway maps only headers the method response declares.
    declared = operation["responses"]["204"]["headers"].keys()
    assert {name.removeprefix("method.response.header.") for name in params} <= declared
    assert {"Access-Control-Allow-Origin", "X-Request-Id"} <= declared


@pytest.mark.parametrize("target", ["aws", "floci"])
def test_the_preflight_is_the_same_on_both_targets(target: Target) -> None:
    assert preflights(extended((SWAGGER_UI,), target)) == preflights(extended((SWAGGER_UI,)))


def test_with_one_allowed_origin_every_gateway_response_names_it() -> None:
    """Gateway Responses can't look at the request, so the value is static (ADR 0014)."""
    for name, response in extended((SWAGGER_UI,))["x-amazon-apigateway-gateway-responses"].items():
        params = response["responseParameters"]
        assert params["gatewayresponse.header.Access-Control-Allow-Origin"] == f"'{SWAGGER_UI}'"
        assert params["gatewayresponse.header.Access-Control-Expose-Headers"] == f"'{EXPOSED}'"
        assert params["gatewayresponse.header.Vary"] == "'Origin'", name


def test_with_several_allowed_origins_gateway_responses_name_none() -> None:
    responses = extended((SWAGGER_UI, APP))["x-amazon-apigateway-gateway-responses"]

    assert "Access-Control" not in str(responses)
