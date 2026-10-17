"""Cross-origin requests from browser clients (ADR 0014).

A browser lets a page read a response from another origin only if the response names the page's
origin in `Access-Control-Allow-Origin`. The API names it only for the origins its environment
allows, and adds nothing at all where none are allowed, so clients that aren't browsers, and
other origins, get the same responses as without CORS. Tokens travel in a header the page sets
itself, never in cookies, so no response allows credentials.

The infrastructure reuses these values for the preflight API Gateway answers, its Gateway
Responses and WAF's custom `429`, so every layer allows the same.
"""

from collections.abc import Collection, Mapping, Sequence

# The API function's environment variable: the allowed origins, comma-separated.
ALLOWED_ORIGINS_VARIABLE = "CORS_ALLOWED_ORIGINS"

ORIGIN_HEADER = "Origin"
ALLOW_ORIGIN_HEADER = "Access-Control-Allow-Origin"

# What a page may send beyond the CORS-safelisted request headers; `Content-Type` too, because
# `application/json` isn't safelisted.
ALLOWED_REQUEST_HEADERS = ("Authorization", "Content-Type", "Idempotency-Key", "If-Match")
# What a page may read beyond the CORS-safelisted response headers (ADRs 0004, 0006 and 0007).
EXPOSED_RESPONSE_HEADERS = (
    "ETag",
    "RateLimit-Limit",
    "RateLimit-Remaining",
    "RateLimit-Reset",
    "Retry-After",
    "X-Request-Id",
)
# How long a browser may reuse a preflight's answer for one URL. Chromium caps it at two hours.
PREFLIGHT_MAX_AGE_SECONDS = 7200


def allowed_origins(value: str | None) -> frozenset[str]:
    """The origins named in `CORS_ALLOWED_ORIGINS`; none for a missing or empty value."""
    return frozenset(origin.strip() for origin in (value or "").split(",") if origin.strip())


def request_origin(headers: Mapping[str, str] | None) -> str | None:
    """The `Origin` header in any casing: REST APIs pass the client's (HTTP/2 lower-cases it)."""
    wanted = ORIGIN_HEADER.lower()
    return next((value for name, value in (headers or {}).items() if name.lower() == wanted), None)


def cors_headers(allowed: Collection[str], origin: str | None) -> dict[str, str]:
    """The CORS headers of a response to a request from `origin`.

    Nothing where no origin is allowed. Otherwise always `Vary: Origin`, since the answer depends
    on the origin, and for an allowed one the origin itself and the headers its page may read.
    """
    if not allowed:
        return {}
    headers = {"Vary": ORIGIN_HEADER}
    if origin is not None and origin in allowed:
        headers[ALLOW_ORIGIN_HEADER] = origin
        headers["Access-Control-Expose-Headers"] = ", ".join(EXPOSED_RESPONSE_HEADERS)
    return headers


def static_cors_headers(allowed: Sequence[str]) -> dict[str, str]:
    """The CORS headers of a response that can't look at the request: API Gateway's Gateway
    Responses and WAF's `429` take static values only. With exactly one allowed origin they name
    it, which only that origin's pages can use; with several they can't choose and carry none."""
    if len(allowed) != 1:
        return {}
    return cors_headers(allowed, allowed[0])
