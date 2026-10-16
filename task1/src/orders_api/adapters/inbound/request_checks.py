"""Request checks the API function makes before any data access (ADR 0007; pipeline in ADR 0009).

Each check raises `InvalidRequest`, which the API handler turns into Problem Details. Body and
parameter schemas come from the OpenAPI document, so the function enforces exactly what the
gateway enforces (Powertools Validation, draft-04).
"""

import base64
import binascii
import json
from collections.abc import Sequence
from typing import Any, NoReturn

from aws_lambda_powertools.utilities.data_classes.common import BaseProxyEvent
from aws_lambda_powertools.utilities.validation import SchemaValidationError, validate

from orders_api.adapters.inbound.http_headers import JSON_MEDIA_TYPE
from orders_api.adapters.inbound.problems import FieldError, ProblemType, json_pointer

MAX_BODY_BYTES = 64 * 1024
_ROOT = "data"  # how fastjsonschema names the validated value in paths and messages


class InvalidRequest(Exception):
    """The request breaks the contract; reported as one Problem Details response."""

    def __init__(
        self, problem: ProblemType, detail: str, errors: Sequence[FieldError] = ()
    ) -> None:
        super().__init__(detail)
        self.problem = problem
        self.detail = detail
        self.errors = tuple(errors)


def _media_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _body_bytes(event: BaseProxyEvent) -> bytes:
    body = event.body or ""
    if not event.is_base64_encoded:
        return body.encode()
    try:
        return base64.b64decode(body, validate=True)
    except binascii.Error:
        raise InvalidRequest(
            ProblemType.VALIDATION_ERROR, "The request body is not valid JSON"
        ) from None


def _reject_constant(constant: str) -> NoReturn:
    raise ValueError(f"{constant} is not JSON")


def json_body(event: BaseProxyEvent) -> Any:
    """The parsed JSON body, after the `Content-Type` (415) and size (413) checks."""
    if _media_type(event.headers.get("Content-Type")) != JSON_MEDIA_TYPE:
        raise InvalidRequest(
            ProblemType.UNSUPPORTED_MEDIA_TYPE,
            f"Send the request body as {JSON_MEDIA_TYPE}",
        )
    raw = _body_bytes(event)
    if len(raw) > MAX_BODY_BYTES:
        raise InvalidRequest(
            ProblemType.PAYLOAD_TOO_LARGE,
            f"The request body must not exceed {MAX_BODY_BYTES // 1024} KB",
        )
    if not raw.strip():
        raise InvalidRequest(ProblemType.VALIDATION_ERROR, "The request body is empty")
    try:
        return json.loads(raw, parse_constant=_reject_constant)
    except ValueError:  # includes JSONDecodeError and UnicodeDecodeError
        raise InvalidRequest(
            ProblemType.VALIDATION_ERROR, "The request body is not valid JSON"
        ) from None


def _field_error(error: SchemaValidationError) -> tuple[str, FieldError]:
    """fastjsonschema's `data.items[0].quantity must …` as a `$.items[0].quantity` message."""
    name = error.name or _ROOT
    message = error.validation_message or ""
    reason = message.removeprefix(name).strip()
    path = list(error.path or [_ROOT])[1:]
    location = "$" + name.removeprefix(_ROOT)
    return f"{location}: {reason}", FieldError(pointer=json_pointer(path), detail=reason)


def validate_body(body: Any, schema: dict[str, Any]) -> None:
    """The gateway's own check, repeated: the identical schema from the OpenAPI document."""
    try:
        validate(event=body, schema=schema)
    except SchemaValidationError as error:
        message, field_error = _field_error(error)
        raise InvalidRequest(
            ProblemType.VALIDATION_ERROR, f"Invalid request body: {message}", [field_error]
        ) from None


def validate_path_parameter(name: str, value: str, schema: dict[str, Any]) -> str:
    """The gateway only checks that path parameters exist; their format is checked here."""
    try:
        validate(event=value, schema=schema)
    except SchemaValidationError as error:
        _, field_error = _field_error(error)
        raise InvalidRequest(
            ProblemType.VALIDATION_ERROR,
            f"Invalid path parameter: {name}",
            [FieldError(parameter=name, detail=field_error.detail)],
        ) from None
    return value
