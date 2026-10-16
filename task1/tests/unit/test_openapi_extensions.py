import copy
import json
from typing import Any

from orders_api.adapters.inbound.http_headers import SECURITY_HEADERS
from orders_api_infra.openapi_extensions import load_spec, with_aws_extensions
from orders_api_infra.paths import SPEC_PATH

LAMBDA_URI = "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/fn/invocations"
AUTHORIZER_URI = "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/auth/invocations"


def extended(spec: dict[str, Any] | None = None) -> dict[str, Any]:
    return with_aws_extensions(
        spec or load_spec(), api_integration_uri=LAMBDA_URI, authorizer_uri=AUTHORIZER_URI
    )


def operations(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        operation
        for path_item in spec["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]


def test_published_spec_stays_free_of_aws_details() -> None:
    spec = load_spec()
    original = copy.deepcopy(spec)

    extended(spec)

    assert spec == original
    assert "x-amazon-apigateway" not in SPEC_PATH.read_text()


def test_every_operation_proxies_to_the_api_function() -> None:
    definition = extended()

    for operation in operations(definition):
        assert operation["x-amazon-apigateway-integration"] == {
            "type": "aws_proxy",
            "httpMethod": "POST",
            "uri": LAMBDA_URI,
            "passthroughBehavior": "when_no_match",
        }


def test_unknown_paths_become_problem_404_instead_of_the_gateway_403() -> None:
    responses = extended()["x-amazon-apigateway-gateway-responses"]

    missing_token = responses["MISSING_AUTHENTICATION_TOKEN"]
    assert missing_token["statusCode"] == "404"
    body = json.loads(
        missing_token["responseTemplates"]["application/json"].replace(
            "$context.requestId", "req-1"
        )
    )
    assert body["type"] == "urn:problem:orders-api:not-found"
    assert body["requestId"] == "req-1"


def test_every_gateway_response_carries_the_response_contract_headers() -> None:
    responses = extended()["x-amazon-apigateway-gateway-responses"]

    assert {"INTEGRATION_FAILURE", "INTEGRATION_TIMEOUT", "DEFAULT_4XX", "DEFAULT_5XX"} <= set(
        responses
    )
    for response in responses.values():
        params = response["responseParameters"]
        assert params["gatewayresponse.header.Content-Type"] == "'application/problem+json'"
        assert params["gatewayresponse.header.X-Request-Id"] == "context.requestId"
        for name, value in SECURITY_HEADERS.items():
            assert params[f"gatewayresponse.header.{name}"] == f"'{value}'"


def test_bearer_scheme_becomes_the_cached_request_authorizer() -> None:
    scheme = extended()["components"]["securitySchemes"]["bearerAuth"]

    assert scheme["type"] == "apiKey"
    assert scheme["in"] == "header"
    assert scheme["name"] == "Authorization"
    assert scheme["x-amazon-apigateway-authtype"] == "custom"
    assert scheme["x-amazon-apigateway-authorizer"] == {
        "type": "request",
        "identitySource": "method.request.header.Authorization",
        "authorizerUri": AUTHORIZER_URI,
        "authorizerResultTtlInSeconds": 300,
    }


def test_published_scheme_stays_plain_bearer() -> None:
    assert load_spec()["components"]["securitySchemes"]["bearerAuth"]["type"] == "http"


def test_rejected_tokens_get_a_problem_401_with_www_authenticate() -> None:
    unauthorized = extended()["x-amazon-apigateway-gateway-responses"]["UNAUTHORIZED"]

    assert unauthorized["statusCode"] == "401"
    params = unauthorized["responseParameters"]
    assert params["gatewayresponse.header.WWW-Authenticate"] == "'Bearer error=\"invalid_token\"'"
    body = json.loads(unauthorized["responseTemplates"]["application/json"])
    assert body["type"] == "urn:problem:orders-api:unauthorized"


def test_authorizer_failures_are_problem_503s() -> None:
    responses = extended()["x-amazon-apigateway-gateway-responses"]

    for name in ("AUTHORIZER_FAILURE", "AUTHORIZER_CONFIGURATION_ERROR"):
        assert responses[name]["statusCode"] == "503"
        body = json.loads(responses[name]["responseTemplates"]["application/json"])
        assert body["type"] == "urn:problem:orders-api:service-unavailable"
