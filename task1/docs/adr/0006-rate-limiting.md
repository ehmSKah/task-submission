---
status: accepted
---

# Rate limiting: per-IP and per-user fixed windows in the API function, WAF as the edge layer

Rate limiting has **two layers**, each doing what it is built for:
- **The API Lambda function** enforces the precise limits the assignment asks for: **per client IP, then per authenticated user**, with fixed 60-second windows counted atomically in DynamoDB, returning `429`.
- **An AWS WAF rate-based rule** on the API stage stops floods per IP at the edge. It runs before the authorizer, so it also covers unauthenticated and invalid-token traffic, which never reaches the API function ([ADR 0003](0003-access-token-validation.md)).

Both layers are defined in CDK and deployed everywhere. The same stack deploys unchanged to AWS, where WAF is enforced; on Floci it is deployed and validated but not enforced.

## Decision

**Placement.**
- Inside the API function, on every request that reaches it: **per-IP limit first, then per-user limit** (the user is the `sub` from the authorizer context).
- `GET /api/v1/health` is not limited in the app.
- Nothing is counted in the authorizer: it can't return `429`, and a Deny would be cached per token for 5 minutes.

**Algorithm: fixed window counter.**
- **Atomic check:** each check is a single DynamoDB `UpdateItem`, `ADD count 1`, with the condition `attribute_not_exists(count) OR count < :limit`. A failed condition means the limit is reached.
- **Rejected requests don't increment the counter,** so a client that keeps hammering can't push its own window further out.
- **Cost:** two writes per request (IP, user). The updated count (`ReturnValues=UPDATED_NEW`) feeds the response headers.

**Counter store: DynamoDB, its own table** ([ADR 0005](0005-dynamodb-storage.md)).
- **Key:** the partition key is `<scope>#<subject>#<windowStart>`, e.g. `ip#203.0.113.7#2026-10-17T09:31Z` or `user#<sub>#2026-10-17T09:31Z`.
- **Attributes:** `count`, and the TTL attribute `expiresAt` (window end plus a margin; TTL deletes lazily, which doesn't matter because the window is part of the key).
- **Settings:** on-demand, no backups, destroyed locally.

**Limits** (defaults per environment):

| Limit | Value per 60 s |
|---|---|
| Per IP | 300 (generous: many users can share one corporate NAT or proxy IP) |
| Per user, `USER` | 60 |
| Per user, `ORDER_MANAGER` | 300 |
| Per user, `ADMIN` | 300 |

A person with several roles gets the highest tier (roles combine, [ADR 0001](0001-role-based-access-control.md)). One budget covers all routes.

**Configuration, changeable at runtime.**
- **Storage:** limits live in **one JSON SSM parameter per environment** (`/orders-api/<env>/rate-limits`), created by CDK with the defaults above.
- **Reading:** the API function reads it through a small *rate-limit config source* interface (Powertools Parameters) and caches it for **60 seconds**, so operational changes take effect within about a minute, with no code change or deploy.
- **Validation:** every value is validated against a schema. An invalid value is rejected and logged, and the **last known good** configuration (or the defaults) stays in force.
- **Ownership:** **CDK owns the initial value and operations own the current one.** A redeploy only overwrites an operator's value when the default in code changes, which is then a deliberate reset.

**Client IP.**
- **Source:** the per-IP key is API Gateway's `requestContext.identity.sourceIp`. `X-Forwarded-For` is never honoured: clients can set it freely, and Floci passes it through untouched.
- **IPv4** is counted per address; **IPv6 per `/64` prefix**, because one host or user typically controls a whole `/64`.

**Responses.**
- **Limit exceeded:** `429 Too Many Requests` with `Retry-After` (seconds until the window resets) and the standard error body, which names the scope that was hit (`ip` or `user`).
- **Every rate-limited response** carries `RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset`, describing the more restrictive of the two limits.

**When the counter store doesn't answer: fail open, with a fallback.**
- **Time budget:** each counter call gets about **200 ms**.
- **Throttling on a counter key** (`ProvisionedThroughputExceededException` / `ThrottlingException`) is treated as **limit exceeded → `429`**. It almost always means one client is hammering its own key, and letting it through would reward the abuse.
- **Timeouts, network and service errors** → the request is allowed. The check falls back to an **in-memory fixed window per Lambda instance** (same limits, a bounded number of keys), and a structured `WARNING` log line with the fixed event name `rate_limiter_unavailable` is written (`rate_limiter_circuit_open` when the breaker opens). In production, a CloudWatch metric filter plus an alarm on those lines makes them alertable without code changes ([ADR 0012](0012-observability-and-security-baseline.md)).
- **Circuit breaker:** after a few consecutive failures the store is skipped for about 30 seconds and only the fallback is used, so requests don't each pay the timeout during an outage. The store is retried automatically afterwards.

**Edge layer: AWS WAF.**
- **What:** a regional web ACL on the REST API stage (also for the edge-optimized endpoint), defined in CDK.
- **The rule:** a rate-based rule aggregating by **`IP`**, at **3,000 requests per 5 minutes**. That is deliberately looser than the app's per-IP budget (300/min, i.e. 1,500 per 5 minutes), so WAF only catches genuine floods and the app stays the precise layer.
- **Blocked requests** get a custom response: `429` with the standard error body and a static `Retry-After`.
- **No forwarded-IP key:** WAF never aggregates on a forwarded-IP header.

## Considered options

- **Counting in the authorizer as well.** It would catch invalid-token floods in-app. *Rejected:* an authorizer can't return `429`, only Allow, Deny (`403`), Unauthorized (`401`) or an error. A Deny is cached per token for 5 minutes, so a throttled user would stay blocked long after their window resets.
- **Algorithms.**
  - *Sliding window counter* (weighted current and previous window): smoother, at one extra read per check. It is the step to take if the boundary burst matters.
  - *Token bucket / GCRA:* explicit burst allowances, but read-modify-write with retries under contention on hot keys.
  - *Sliding window log:* exact, but one write per request timestamp. Too costly.
- **Counter stores.**
  - *ElastiCache Serverless (Valkey/Redis):* sub-millisecond and built for hot keys, but Lambda would have to run in the VPC, and Floci support is unverified. It is the production step when latency or cost per request matter.
  - *In-memory only:* each Lambda instance would count alone, so it is useless as the primary store (kept only as the outage fallback).
- **Configuration.**
  - *CDK config and environment variables only:* every change would need a deploy.
  - *AWS AppConfig:* JSON-Schema validators reject bad config before it goes live, and deployment strategies roll out gradually and roll back automatically on an alarm. That makes it the better tool for operational config. *Rejected for now* because Floci has no CloudFormation support for `AWS::AppConfig::*`, so the local stack couldn't be deployed by CDK alone. Because the config source is an interface, adopting AppConfig is a new adapter, not a redesign.
- **Failure modes.**
  - *Fail closed:* a counter-table hiccup would become a full API outage.
  - *Plain fail open without a fallback:* no limits at all during an outage.
- **Response headers.**
  - *None:* clients can't see their budget before hitting `429`.
  - *The current IETF draft structured fields* (`RateLimit-Policy`, `RateLimit`): more precise and multi-policy, but still a draft and less familiar. This is where the headers should move once the standard settles.
- **Per-route limits** (e.g. stricter for `POST`): a useful extension, not worth a second configuration dimension now.
- **Honouring `X-Forwarded-For` / WAF's forwarded-IP key:** only correct behind a proxy we operate. Otherwise trivially spoofed.

## Consequences

- **Floods of invalid tokens are not limited locally.** On Floci, WAF is deployed and validated but **not enforced** (Floci emulates WAF's management API only). On AWS, the same CDK stack enforces it.
- **Boundary burst:** a client can send up to twice its limit in a short burst across a window boundary. This is the documented trade-off of fixed windows.
- **Cost and latency:** two DynamoDB writes per request, on the order of the REST API's own price per million requests (the exact figure is in the cost model, from current AWS pricing; [ADR 0013](0013-production-readiness-and-evolution.md)), and about 10–20 ms of added latency. **Hot keys:** a single abusive IP concentrates on one partition key (about 1,000 writes/s, and rejected writes still consume capacity). WAF absorbs that in production.
- **Limits are approximate during a counter-store outage:** with the in-memory fallback, the effective limit is the configured limit times the number of warm Lambda instances.
- **To verify on the first real AWS deploy:**
  - that WAF's `IP` key and `sourceIp` both see the client's address for the edge-optimized endpoint. This is stated in an AWS re:Post expert article, but not clearly in the official documentation.
  - how WAF aggregates IPv6 addresses. The app counts IPv6 per `/64`.
- **A CDN of our own in front later** (regional endpoint plus our own CloudFront): WAF moves to CloudFront (scope `CLOUDFRONT`), and the app's per-IP key switches to the client address appended by that trusted proxy (a configured number of hops counted from the right, never the leftmost value).
- **Local tests** all come from one Docker IP, so per-IP behaviour is covered mainly by unit tests, and the per-IP integration test uses its own configuration.
