"""RFC 9457 Problem Details: the one error shape every layer returns (ADR 0007)."""

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


def problem_body(
    problem: ProblemType,
    *,
    request_id: str,
    instance: str | None = None,
    detail: str | None = None,
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
    return body
