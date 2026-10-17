"""Turn the clean OpenAPI contract into an API Gateway definition at synth time (ADR 0008).

The published document stays free of AWS details; this module adds the `x-amazon-apigateway-*`
extensions that make the same file define the gateway's routes and error responses.
"""

import copy
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from orders_api.adapters.inbound.cors import (
    ALLOW_ORIGIN_HEADER,
    ALLOWED_REQUEST_HEADERS,
    ORIGIN_HEADER,
    PREFLIGHT_MAX_AGE_SECONDS,
    static_cors_headers,
)
from orders_api.adapters.inbound.http_headers import (
    PROBLEM_JSON_MEDIA_TYPE,
    REQUEST_ID_HEADER,
    SECURITY_HEADERS,
)
from orders_api.adapters.inbound.problems import ProblemType, problem_body
from orders_api.contract import load_contract
from orders_api_infra.config import Target

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


# A browser's CORS preflight is answered by API Gateway itself through a MOCK integration, so it
# needs no token, invokes no function and spends no rate limit (ADR 0014).
PREFLIGHT_STATUS = 204


def _override(header: str, value: str) -> str:
    """A VTL line setting a response header. By index, because Floci 2.3.0's Velocity can't parse
    a hyphenated name in `$context.responseOverride.header.Name` (docs/verification.md)."""
    return f"#set($context.responseOverride.header['{header}'] = {value})"


def _preflight_access_control(methods: str) -> dict[str, str]:
    """The headers a preflight from an allowed origin gets, as VTL values."""
    return {
        ALLOW_ORIGIN_HEADER: "$origin",
        "Access-Control-Allow-Methods": f"'{methods}'",
        "Access-Control-Allow-Headers": f"'{', '.join(ALLOWED_REQUEST_HEADERS)}'",
        "Access-Control-Max-Age": f"'{PREFLIGHT_MAX_AGE_SECONDS}'",
    }


def _preflight_template(allowed_origins: Sequence[str], access_control: dict[str, str]) -> str:
    """VTL for the preflight's answer: the request id always; the `Access-Control-*` headers
    only for an allowed origin, echoed. The `Origin` header is looked up in any casing, because
    HTTP/2 clients send it lower-cased and Floci's `$input.params('Origin')` matches exactly.
    The origins are validated (config.py), so they can't break out of the quotes."""
    allowed = " || ".join(f"$origin == '{origin}'" for origin in allowed_origins)
    lines = [
        "#set($origin = '')",
        "#foreach($name in $input.params().header.keySet())",
        f"#if($name.equalsIgnoreCase('{ORIGIN_HEADER}'))"
        "#set($origin = $input.params().header.get($name))#end",
        "#end",
        _override(REQUEST_ID_HEADER, "$context.requestId"),
        f"#if({allowed})",
        *(_override(name, value) for name, value in access_control.items()),
        "#end",
    ]
    return "\n".join(lines) + "\n"


def _preflight(path_item: dict[str, Any], allowed_origins: Sequence[str]) -> dict[str, Any]:
    """The `OPTIONS` operation answering a CORS preflight for one path."""
    methods = ", ".join(sorted(method.upper() for method in path_item if method in HTTP_METHODS))
    access_control = _preflight_access_control(methods)
    static_headers = {**SECURITY_HEADERS, "Vary": ORIGIN_HEADER}
    template = _preflight_template(allowed_origins, access_control)
    # API Gateway maps only the headers the method response declares.
    declared = [*static_headers, REQUEST_ID_HEADER, *access_control]
    return {
        "security": [],
        "responses": {
            str(PREFLIGHT_STATUS): {
                "description": "CORS preflight",
                "headers": {name: {"schema": {"type": "string"}} for name in declared},
            }
        },
        "x-amazon-apigateway-integration": {
            "type": "mock",
            "requestTemplates": {"application/json": json.dumps({"statusCode": PREFLIGHT_STATUS})},
            "passthroughBehavior": "when_no_match",
            "responses": {
                "default": {
                    "statusCode": str(PREFLIGHT_STATUS),
                    "responseParameters": {
                        f"method.response.header.{name}": f"'{value}'"
                        for name, value in static_headers.items()
                    },
                    "responseTemplates": {"application/json": template},
                }
            },
        },
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


def _gateway_response(
    error: GatewayError, target: Target, cors_allowed_origins: Sequence[str]
) -> dict[str, Any]:
    static_headers = {
        **SECURITY_HEADERS,
        **error.extra_headers,
        **static_cors_headers(cors_allowed_origins),
    }
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
    """The clean, published OpenAPI document (a private copy of the function's own contract)."""
    return copy.deepcopy(load_contract())


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
    spec: dict[str, Any],
    *,
    api_integration_uri: str,
    authorizer_uri: str,
    target: Target,
    cors_allowed_origins: Sequence[str] = (),
) -> dict[str, Any]:
    """Return a copy of `spec` with the API Gateway extensions added for `target`, and with CORS
    for browser clients from `cors_allowed_origins`, if any (ADR 0014)."""
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
        if cors_allowed_origins:
            path_item["options"] = _preflight(path_item, cors_allowed_origins)
    definition["x-amazon-apigateway-request-validators"] = copy.deepcopy(REQUEST_VALIDATORS)
    definition["x-amazon-apigateway-gateway-responses"] = {
        name: _gateway_response(error, target, cors_allowed_origins)
        for name, error in GATEWAY_ERRORS.items()
    }
    return definition
