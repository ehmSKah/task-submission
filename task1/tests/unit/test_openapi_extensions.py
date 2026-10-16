import copy
import json
from typing import Any

from orders_api.adapters.inbound.http_headers import SECURITY_HEADERS
from orders_api_infra.openapi_extensions import load_spec, with_aws_extensions
from orders_api_infra.paths import SPEC_PATH

LAMBDA_URI = "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/fn/invocations"


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

    with_aws_extensions(spec, api_integration_uri=LAMBDA_URI)

    assert spec == original
    assert "x-amazon-apigateway" not in SPEC_PATH.read_text()


def test_every_operation_proxies_to_the_api_function() -> None:
    definition = with_aws_extensions(load_spec(), api_integration_uri=LAMBDA_URI)

    for operation in operations(definition):
        assert operation["x-amazon-apigateway-integration"] == {
            "type": "aws_proxy",
            "httpMethod": "POST",
            "uri": LAMBDA_URI,
            "passthroughBehavior": "when_no_match",
        }


def test_unknown_paths_become_problem_404_instead_of_the_gateway_403() -> None:
    responses = with_aws_extensions(load_spec(), api_integration_uri=LAMBDA_URI)[
        "x-amazon-apigateway-gateway-responses"
    ]

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
    responses = with_aws_extensions(load_spec(), api_integration_uri=LAMBDA_URI)[
        "x-amazon-apigateway-gateway-responses"
    ]

    assert {"INTEGRATION_FAILURE", "INTEGRATION_TIMEOUT", "DEFAULT_4XX", "DEFAULT_5XX"} <= set(
        responses
    )
    for response in responses.values():
        params = response["responseParameters"]
        assert params["gatewayresponse.header.Content-Type"] == "'application/problem+json'"
        assert params["gatewayresponse.header.X-Request-Id"] == "context.requestId"
        for name, value in SECURITY_HEADERS.items():
            assert params[f"gatewayresponse.header.{name}"] == f"'{value}'"
