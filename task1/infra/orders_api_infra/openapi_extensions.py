"""Turn the clean OpenAPI contract into an API Gateway definition at synth time (ADR 0008).

The published document stays free of AWS details; this module adds the `x-amazon-apigateway-*`
extensions that make the same file define the gateway's routes and error responses.
"""

import copy
import json
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

# Gateway Response type -> the problem it reports. `None` means a generic `about:blank`
# problem, used where the gateway decides the status code itself.
GATEWAY_PROBLEMS: dict[str, ProblemType | None] = {
    "MISSING_AUTHENTICATION_TOKEN": ProblemType.NOT_FOUND,
    "INTEGRATION_FAILURE": ProblemType.BAD_GATEWAY,
    "INTEGRATION_TIMEOUT": ProblemType.GATEWAY_TIMEOUT,
    "DEFAULT_4XX": None,
    "DEFAULT_5XX": None,
}


def _problem_template(problem: ProblemType | None) -> str:
    if problem is None:
        # $context.error.messageString is already a quoted JSON string.
        return (
            '{"type":"about:blank","title":$context.error.messageString,'
            '"requestId":"$context.requestId"}'
        )
    return json.dumps(problem_body(problem, request_id="$context.requestId"), separators=(",", ":"))


def _gateway_response(problem: ProblemType | None) -> dict[str, Any]:
    headers = {
        f"gatewayresponse.header.{name}": f"'{value}'" for name, value in SECURITY_HEADERS.items()
    }
    headers["gatewayresponse.header.Content-Type"] = f"'{PROBLEM_JSON_MEDIA_TYPE}'"
    headers[f"gatewayresponse.header.{REQUEST_ID_HEADER}"] = "context.requestId"
    response: dict[str, Any] = {
        "responseParameters": headers,
        "responseTemplates": {"application/json": _problem_template(problem)},
    }
    if problem is not None:
        response["statusCode"] = str(problem.status)
    return response


def load_spec() -> dict[str, Any]:
    """The clean, published OpenAPI document."""
    spec: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text())
    return spec


def with_aws_extensions(spec: dict[str, Any], *, api_integration_uri: str) -> dict[str, Any]:
    """Return a copy of `spec` with the API Gateway extensions added."""
    definition = copy.deepcopy(spec)
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
        name: _gateway_response(problem) for name, problem in GATEWAY_PROBLEMS.items()
    }
    return definition
