"""Turn the clean OpenAPI contract into an API Gateway definition at synth time (ADR 0008).

The published document stays free of AWS details; this module adds the `x-amazon-apigateway-*`
extensions that make the same file define the gateway's routes and error responses.
"""

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Literal

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

# Bodies are validated against their schema at the gateway, so an invalid body never invokes the
# API function. Parameters are only checked for presence there; their format is checked in code
# (ADR 0007).
BODY_AND_PARAMETERS = "body-and-parameters"
PARAMETERS = "parameters"
REQUEST_VALIDATORS = {
    BODY_AND_PARAMETERS: {"validateRequestBody": True, "validateRequestParameters": True},
    PARAMETERS: {"validateRequestBody": False, "validateRequestParameters": True},
}

Target = Literal["floci", "aws"]

# The request validator's message goes into `detail`. AWS Gateway Response templates allow
# variable substitution only, no VTL, and AWS inserts this message JSON-escaped. Floci 2.3.0
# inserts it raw (an enum list alone carries quotes) but evaluates VTL, so there it is escaped
# explicitly; `escapeJavaScript` also escapes `'`, which JSON doesn't allow, so that is undone.
# See docs/verification.md.
VALIDATOR_MESSAGE = "$context.error.validationErrorString"
VALIDATOR_MESSAGE_IN_JSON: dict[Target, str] = {
    "aws": VALIDATOR_MESSAGE,
    "floci": f"$util.escapeJavaScript({VALIDATOR_MESSAGE})" + """.replaceAll("\\\\'","'")""",
}
_DETAIL_PLACEHOLDER = "__validator_message__"


@dataclass(frozen=True)
class GatewayError:
    """How one API Gateway response type is reported. `problem=None` means a generic
    `about:blank` problem, used where the gateway decides the status code itself."""

    problem: ProblemType | None
    extra_headers: dict[str, str] = field(default_factory=dict)
    validator_message: bool = False
    """Whether `detail` carries the request validator's message."""


GATEWAY_ERRORS: dict[str, GatewayError] = {
    "BAD_REQUEST_BODY": GatewayError(ProblemType.VALIDATION_ERROR, validator_message=True),
    "BAD_REQUEST_PARAMETERS": GatewayError(ProblemType.VALIDATION_ERROR, validator_message=True),
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


def _problem_template(error: GatewayError, target: Target) -> str:
    if error.problem is None:
        # $context.error.messageString is already a quoted JSON string.
        return (
            '{"type":"about:blank","title":$context.error.messageString,'
            '"requestId":"$context.requestId"}'
        )
    detail = _DETAIL_PLACEHOLDER if error.validator_message else None
    body = problem_body(error.problem, detail=detail, request_id="$context.requestId")
    # Spliced in after encoding, because json.dumps would escape the expression's own quotes.
    return json.dumps(body, separators=(",", ":")).replace(
        _DETAIL_PLACEHOLDER, VALIDATOR_MESSAGE_IN_JSON[target]
    )


def _gateway_response(error: GatewayError, target: Target) -> dict[str, Any]:
    static_headers = {**SECURITY_HEADERS, **error.extra_headers}
    headers = {
        f"gatewayresponse.header.{name}": f"'{value}'" for name, value in static_headers.items()
    }
    headers["gatewayresponse.header.Content-Type"] = f"'{PROBLEM_JSON_MEDIA_TYPE}'"
    headers[f"gatewayresponse.header.{REQUEST_ID_HEADER}"] = "context.requestId"
    response: dict[str, Any] = {
        "responseParameters": headers,
        "responseTemplates": {"application/json": _problem_template(error, target)},
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
    spec: dict[str, Any], *, api_integration_uri: str, authorizer_uri: str, target: Target
) -> dict[str, Any]:
    """Return a copy of `spec` with the API Gateway extensions added for `target`."""
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
                operation["x-amazon-apigateway-request-validator"] = (
                    BODY_AND_PARAMETERS if "requestBody" in operation else PARAMETERS
                )
    definition["x-amazon-apigateway-request-validators"] = copy.deepcopy(REQUEST_VALIDATORS)
    definition["x-amazon-apigateway-gateway-responses"] = {
        name: _gateway_response(error, target) for name, error in GATEWAY_ERRORS.items()
    }
    return definition
