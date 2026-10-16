"""Turn the clean OpenAPI contract into an API Gateway definition at synth time (ADR 0008).

The published document stays free of AWS details; this module adds the `x-amazon-apigateway-*`
extensions that make the same file define the gateway's routes and error responses.
"""

import copy
import json
from dataclasses import dataclass, field
from typing import Any

import yaml

from orders_api.adapters.inbound.http_headers import (
    PROBLEM_JSON_MEDIA_TYPE,
    REQUEST_ID_HEADER,
    SECURITY_HEADERS,
)
from orders_api.adapters.inbound.problems import ProblemType, problem_body
from orders_api_infra.paths import SPEC_PATH

HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch"}
BEARER_SCHEME = "bearerAuth"
AUTHORIZER_RESULT_TTL_SECONDS = 300


@dataclass(frozen=True)
class GatewayError:
    """How one API Gateway response type is reported. `problem=None` means a generic
    `about:blank` problem, used where the gateway decides the status code itself."""

    problem: ProblemType | None
    extra_headers: dict[str, str] = field(default_factory=dict)


GATEWAY_ERRORS: dict[str, GatewayError] = {
    "MISSING_AUTHENTICATION_TOKEN": GatewayError(ProblemType.NOT_FOUND),
    # One generic 401 for every rejected token; the reason is only logged (ADR 0003).
    "UNAUTHORIZED": GatewayError(
        ProblemType.UNAUTHORIZED, {"WWW-Authenticate": 'Bearer error="invalid_token"'}
    ),
    # The authorizer failed (e.g. the provider's keys are unreachable). AWS may report a
    # failing authorizer function as either type, so both map to 503.
    "AUTHORIZER_FAILURE": GatewayError(ProblemType.SERVICE_UNAVAILABLE),
    "AUTHORIZER_CONFIGURATION_ERROR": GatewayError(ProblemType.SERVICE_UNAVAILABLE),
    "INTEGRATION_FAILURE": GatewayError(ProblemType.BAD_GATEWAY),
    "INTEGRATION_TIMEOUT": GatewayError(ProblemType.GATEWAY_TIMEOUT),
    "DEFAULT_4XX": GatewayError(None),
    "DEFAULT_5XX": GatewayError(None),
}


def _problem_template(problem: ProblemType | None) -> str:
    if problem is None:
        # $context.error.messageString is already a quoted JSON string.
        return (
            '{"type":"about:blank","title":$context.error.messageString,'
            '"requestId":"$context.requestId"}'
        )
    return json.dumps(problem_body(problem, request_id="$context.requestId"), separators=(",", ":"))


def _gateway_response(error: GatewayError) -> dict[str, Any]:
    static_headers = {**SECURITY_HEADERS, **error.extra_headers}
    headers = {
        f"gatewayresponse.header.{name}": f"'{value}'" for name, value in static_headers.items()
    }
    headers["gatewayresponse.header.Content-Type"] = f"'{PROBLEM_JSON_MEDIA_TYPE}'"
    headers[f"gatewayresponse.header.{REQUEST_ID_HEADER}"] = "context.requestId"
    response: dict[str, Any] = {
        "responseParameters": headers,
        "responseTemplates": {"application/json": _problem_template(error.problem)},
    }
    if error.problem is not None:
        response["statusCode"] = str(error.problem.status)
    return response


def load_spec() -> dict[str, Any]:
    """The clean, published OpenAPI document."""
    spec: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text())
    return spec


def _lambda_authorizer(authorizer_uri: str) -> dict[str, Any]:
    """The REQUEST authorizer, in the `apiKey` form API Gateway's OpenAPI import expects.

    The published document describes the same scheme as plain HTTP bearer for clients.
    """
    return {
        "type": "apiKey",
        "name": "Authorization",
        "in": "header",
        "x-amazon-apigateway-authtype": "custom",
        "x-amazon-apigateway-authorizer": {
            "type": "request",
            "identitySource": "method.request.header.Authorization",
            "authorizerUri": authorizer_uri,
            "authorizerResultTtlInSeconds": AUTHORIZER_RESULT_TTL_SECONDS,
        },
    }


def with_aws_extensions(
    spec: dict[str, Any], *, api_integration_uri: str, authorizer_uri: str
) -> dict[str, Any]:
    """Return a copy of `spec` with the API Gateway extensions added."""
    definition = copy.deepcopy(spec)
    definition["components"]["securitySchemes"][BEARER_SCHEME] = _lambda_authorizer(authorizer_uri)
    for path_item in definition["paths"].values():
        for method, operation in path_item.items():
            if method in HTTP_METHODS:
                operation["x-amazon-apigateway-integration"] = {
                    "type": "aws_proxy",
                    "httpMethod": "POST",
                    "uri": api_integration_uri,
                    "passthroughBehavior": "when_no_match",
                }
    definition["x-amazon-apigateway-gateway-responses"] = {
        name: _gateway_response(error) for name, error in GATEWAY_ERRORS.items()
    }
    return definition
