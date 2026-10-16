"""RFC 9457 Problem Details: the one error shape every layer returns (ADR 0007)."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

URN_PREFIX = "urn:problem:orders-api:"


class ProblemType(Enum):
    """The catalogue of error types; the slug is the machine-readable code."""

    VALIDATION_ERROR = ("validation-error", "Request validation failed", 400)
    UNAUTHORIZED = ("unauthorized", "Missing or invalid access token", 401)
    FORBIDDEN = ("forbidden", "Not allowed", 403)
    NOT_FOUND = ("not-found", "Resource not found", 404)
    CONFLICT = ("conflict", "Conflict with the current state", 409)
    PRECONDITION_FAILED = ("precondition-failed", "Precondition failed", 412)
    PAYLOAD_TOO_LARGE = ("payload-too-large", "Payload too large", 413)
    UNSUPPORTED_MEDIA_TYPE = ("unsupported-media-type", "Unsupported media type", 415)
    IDEMPOTENCY_KEY_MISMATCH = (
        "idempotency-key-mismatch",
        "Idempotency key reused with a different request",
        422,
    )
    RATE_LIMITED = ("rate-limited", "Too many requests", 429)
    INTERNAL_ERROR = ("internal-error", "An unexpected error occurred", 500)
    BAD_GATEWAY = ("bad-gateway", "Bad gateway", 502)
    SERVICE_UNAVAILABLE = ("service-unavailable", "Service unavailable", 503)
    GATEWAY_TIMEOUT = ("gateway-timeout", "Gateway timeout", 504)

    def __init__(self, slug: str, title: str, status: int) -> None:
        self.slug = slug
        self.title = title
        self.status = status

    @property
    def urn(self) -> str:
        return URN_PREFIX + self.slug


@dataclass(frozen=True)
class FieldError:
    """One field-level error found by code: in the body (`pointer`) or a parameter."""

    detail: str
    pointer: str | None = None
    """JSON Pointer (RFC 6901) into the request body."""
    parameter: str | None = None
    """Name of a path or query parameter."""

    def __post_init__(self) -> None:
        if (self.pointer is None) == (self.parameter is None):
            raise ValueError("A field error names exactly one location: pointer or parameter")

    def to_json(self) -> dict[str, str]:
        if self.pointer is not None:
            return {"pointer": self.pointer, "detail": self.detail}
        return {"parameter": str(self.parameter), "detail": self.detail}


def json_pointer(path: Iterable[str | int]) -> str:
    """RFC 6901: `~` and `/` inside a segment are escaped as `~0` and `~1`."""
    return "".join("/" + str(segment).replace("~", "~0").replace("/", "~1") for segment in path)


def problem_body(
    problem: ProblemType,
    *,
    request_id: str,
    instance: str | None = None,
    detail: str | None = None,
    errors: Sequence[FieldError] = (),
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": problem.urn,
        "title": problem.title,
        "status": problem.status,
    }
    if detail is not None:
        body["detail"] = detail
    if instance is not None:
        body["instance"] = instance
    body["requestId"] = request_id
    if errors:
        body["errors"] = [error.to_json() for error in errors]
    return body
