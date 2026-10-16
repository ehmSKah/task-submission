import copy
import json
from typing import Any

from orders_api.adapters.inbound.http_headers import SECURITY_HEADERS
from orders_api_infra.openapi_extensions import Target, load_spec, with_aws_extensions
from orders_api_infra.paths import SPEC_PATH

LAMBDA_URI = "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/fn/invocations"
AUTHORIZER_URI = "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/auth/invocations"


def extended(spec: dict[str, Any] | None = None, target: Target = "aws") -> dict[str, Any]:
    return with_aws_extensions(
        spec or load_spec(),
        api_integration_uri=LAMBDA_URI,
        authorizer_uri=AUTHORIZER_URI,
        target=target,
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


def operations_by_route(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        f"{method.upper()} {path}": operation
        for path, path_item in spec["paths"].items()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }


def test_request_validators_check_bodies_and_parameters() -> None:
    validators = extended()["x-amazon-apigateway-request-validators"]

    assert validators == {
        "body-and-parameters": {"validateRequestBody": True, "validateRequestParameters": True},
        "parameters": {"validateRequestBody": False, "validateRequestParameters": True},
    }


def test_operations_with_a_body_validate_it_at_the_gateway() -> None:
    routes = operations_by_route(extended())

    assert "requestBody" in routes["POST /api/v1/orders"]
    for route, operation in routes.items():
        expected = "body-and-parameters" if "requestBody" in operation else "parameters"
        assert operation["x-amazon-apigateway-request-validator"] == expected, route


VALIDATION_RESPONSES = ("BAD_REQUEST_BODY", "BAD_REQUEST_PARAMETERS")
FLOCI_ESCAPED_MESSAGE = (
    "$util.escapeJavaScript($context.error.validationErrorString)" """.replaceAll("\\\\'","'")"""
)


def validation_templates(target: Target) -> dict[str, str]:
    responses = extended(target=target)["x-amazon-apigateway-gateway-responses"]
    for name in VALIDATION_RESPONSES:
        assert responses[name]["statusCode"] == "400"
    return {
        name: responses[name]["responseTemplates"]["application/json"]
        for name in VALIDATION_RESPONSES
    }


def test_gateway_validation_errors_are_problem_400s_with_the_validator_message() -> None:
    for template in validation_templates("aws").values():
        # AWS substitutes variables only (no VTL) and inserts this message JSON-escaped.
        assert '"detail":"$context.error.validationErrorString"' in template
        body = json.loads(template)
        assert body["type"] == "urn:problem:orders-api:validation-error"
        assert body["status"] == 400
        assert body["requestId"] == "$context.requestId"


def test_on_floci_the_validator_message_is_escaped_with_vtl() -> None:
    """Floci inserts the raw message, which may contain quotes, but evaluates VTL."""
    message = """$: property 'say "hi" \\o/' is not defined in the schema"""
    rendered_by_vtl = json.dumps(message)[1:-1]

    for template in validation_templates("floci").values():
        assert f'"detail":"{FLOCI_ESCAPED_MESSAGE}"' in template
        body = json.loads(template.replace(FLOCI_ESCAPED_MESSAGE, rendered_by_vtl))
        assert body["detail"] == message
        assert body["type"] == "urn:problem:orders-api:validation-error"


def test_only_the_validation_responses_differ_between_targets() -> None:
    aws = extended(target="aws")["x-amazon-apigateway-gateway-responses"]
    floci = extended(target="floci")["x-amazon-apigateway-gateway-responses"]

    assert {name for name in aws if aws[name] != floci[name]} == set(VALIDATION_RESPONSES)


def test_authorizer_failures_are_problem_503s() -> None:
    responses = extended()["x-amazon-apigateway-gateway-responses"]

    for name in ("AUTHORIZER_FAILURE", "AUTHORIZER_CONFIGURATION_ERROR"):
        assert responses[name]["statusCode"] == "503"
        body = json.loads(responses[name]["responseTemplates"]["application/json"])
        assert body["type"] == "urn:problem:orders-api:service-unavailable"
