---
status: accepted
---

# Observability and security baseline: structured logs only, least-privilege IAM, standard security headers

The build includes **what the assignment's requirements touch, and nothing beyond.**

**Built:**
- **Structured logs** carry every operational signal.
- **IAM** is minimal and proven by `cdk-nag`.
- **Every response**, from every layer, carries a standard set of security headers.

**Not built:**
- custom metrics, tracing, alarms and dashboards. The task doesn't ask for them, and Floci can't graph metrics or emulate X-Ray anyway. They are documented as production evolution.

## Decision

**Logging** (Powertools Logger, one JSON line per event):
- **Every line:** `requestId` (API Gateway's, the same as the `X-Request-Id` header, [ADR 0007](0007-validation-and-errors.md)), `function_request_id`, `cold_start`, `level`, `service=orders-api`, `env`.
- **Added once known:**
  - `route` (method + resource template)
  - `sub` and `roles`
- **One access line per request in the API function:** `status`, `latencyMs`, `problemType`, `rateLimitScope` and `clientNetwork` (on `429`: the scope hit, and the client's address truncated as below), `orderId` (when relevant).
- **Authorizer:** the decision, the **precise rejection reason** (returned to nobody, [ADR 0003](0003-access-token-validation.md)), `kid`, `iss`, `client_id`, and whether keys were cached or fetched.
- **Operational events** have fixed event names, e.g.:
  - `rate_limiter_unavailable`, `rate_limiter_circuit_open`, `rate_limit_config_invalid`, `rate_limit_config_unavailable` ([ADR 0006](0006-rate-limiting.md))
  - `jwks_fetched`, `jwks_refetch`, `jwks_fetch_failed`
  - `rate_limiter_circuit_closed` (`INFO`): the counter store answers again after the circuit was open, so an outage's end is visible too ([ADR 0006](0006-rate-limiting.md))
- **Never logged:** tokens or `Authorization` headers, passwords or secrets, request and response bodies.
- **Client IPs in application logs are truncated** (IPv4 `/24`, IPv6 `/64`). IP addresses are personal data under the GDPR. The full address appears only in API Gateway's own access logs (under the retention below) and in rate-limit counter keys, which expire within minutes.
- **Levels:** DEBUG locally, INFO on AWS, set by an environment variable. In `aws-*` environments, **1 % of requests** log at DEBUG (Powertools sampling).
- **API Gateway access logs** (JSON: request id, status, latency, `sourceIp`, authorizer error) are on in `aws-*`, off locally.
- **Retention:** log groups are owned by CDK with explicit retention: **14 days** in `aws-dev`, **90 days** in `aws-prod`, never unlimited.

**Metrics, tracing, alarms: not built.**
- **Logs instead:** the logs above carry every signal. AWS's built-in Lambda, API Gateway, DynamoDB and WAF metrics exist on a real deployment at no extra effort.
- **The fail-open rate limiter** (ADR 0006) is made visible by its `WARNING` event lines. In production, a CloudWatch metric filter plus an alarm on them turns that into an alert without code changes.

**IAM** (hand-written minimal statements on exact ARNs; CDK's broad `grant*` helpers are avoided because they include `Scan` and batch operations):

| Function | Permissions |
|---|---|
| Authorizer | write to its own log group only. The JWKS is fetched over HTTPS, so it needs no AWS permissions. |
| API function | `dynamodb:GetItem`/`PutItem`/`UpdateItem`/`DeleteItem` on `orders`; `dynamodb:Query` on the `byOwner` and `byCreated` index ARNs (named, no `index/*`); `GetItem`/`PutItem`/`UpdateItem`/`DeleteItem` on the idempotency table; `dynamodb:UpdateItem` on the rate-limit table; `ssm:GetParameter` on `/orders-api/<env>/rate-limits`; its own log group |

- **Neither function** gets `Scan`, batch operations, Secrets Manager or KMS.
- **Invocation:** each function's resource policy allows `lambda:InvokeFunction` **only from this REST API**, the authorizer's and the methods' source ARNs ([ADR 0003](0003-access-token-validation.md) relies on this).
- **Test helpers** (emptying the counter table, switching SSM limits, reading the seed secret) run with developer or CI credentials, **never** with a Lambda role.
- **`cdk-nag`** (AWS Solutions pack) runs at synth and in CI. Every deliberate exception is suppressed with a written reason.

**Security headers on every response**, following the OWASP REST Security Cheat Sheet:

| Header | Value |
|---|---|
| `Cache-Control` | `no-store` |
| `Content-Type` | always explicit (`application/json` / `application/problem+json`) |
| `X-Content-Type-Options` | `nosniff` |
| `Content-Security-Policy` | `default-src 'none'; frame-ancestors 'none'` |
| `X-Frame-Options` | `DENY` |
| `Strict-Transport-Security` | `max-age=31536000` (ignored by browsers over Floci's plain HTTP) |

- **No CORS headers** ([ADR 0008](0008-openapi-contract.md)).
- **Where the headers are set:**
  - by a response middleware in the API function, for every response including `/health`
  - as static parameters in every mapped Gateway Response
  - in WAF's custom `429`
- **Tested:** integration tests check the full set on one response from each of those layers (WAF on AWS only).
- **Headers API Gateway adds itself** (`x-amzn-RequestId`, `x-amz-apigw-id`) can't be removed and reveal nothing beyond "AWS".

## Considered options

- **Custom metrics with Powertools (Embedded Metric Format):**
  - business: orders created, status changes
  - security: authentication rejections by reason, `403`, `429` by scope
  - dependency health: limiter fallback, JWKS fetches

  *Rejected for the build:* not a requirement. Floci keeps EMF output as plain log lines, so locally they would add nothing over the logs. Documented as production evolution, with the cardinality rule: never `sub`, order id, IP or request id as a dimension.
- **Calling `PutMetricData` directly** (Floci stores those). It adds an API call on the request path, which EMF exists to avoid.
- **X-Ray tracing via Powertools Tracer.** Not emulated by Floci, and not required. Production evolution.
- **Alarms and dashboards in CDK for `aws-*`.** Not required, and nothing to alarm on locally. Production evolution.
- **Full client IPs in application logs.** Simpler abuse investigation, but more personal data in long-lived logs. API Gateway's access logs keep the full address where it is needed.
- **CDK `grantReadWriteData()` helpers.** Less code, but they grant `Scan`, `BatchWriteItem` and `BatchGetItem`, which nothing here uses.
- **No security headers.** Responses would rely on clients behaving well. The headers are cheap and standard for APIs.

## Consequences

- **The rate limiter's fail-open state** is visible only by reading logs until the production metric filter and alarm exist.
- **Investigating one user's activity** means querying logs (e.g. CloudWatch Logs Insights by `sub`), not dashboards.
- **IAM statements must be updated by hand** when an adapter starts using a new action. `cdk-nag` and the integration tests (failing with `AccessDenied`) catch omissions.
- **Header consistency depends on three places** (the function, Gateway Responses, WAF). The integration tests guard all three.
