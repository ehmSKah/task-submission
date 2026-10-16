"""Headers every response carries, whichever layer produces it (ADR 0012).

The infrastructure reuses these values for API Gateway's Gateway Responses and WAF's custom
response, so the Lambda and the edge stay identical.
"""

JSON_MEDIA_TYPE = "application/json"
PROBLEM_JSON_MEDIA_TYPE = "application/problem+json"

SECURITY_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Strict-Transport-Security": "max-age=31536000",
}

REQUEST_ID_HEADER = "X-Request-Id"
