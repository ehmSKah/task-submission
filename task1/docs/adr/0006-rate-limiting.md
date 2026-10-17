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
  - A request over the IP limit never reaches the user's counter.
  - A request over the user limit has already been counted against its IP, because the IP is checked first. Two independent writes can't be undone together, and an IP's budget is the coarser one anyway.
- **Cost:** two writes per request (IP, user). The updated count (`ReturnValues=UPDATED_NEW`) feeds the response headers.
- **Windows** are the UTC minutes: a window starts on a full minute and lasts 60 seconds, so the IP's and the user's windows always end together.

**Counter store: DynamoDB, its own table** ([ADR 0005](0005-dynamodb-storage.md)).
- **Key:** the partition key is `<scope>#<subject>#<windowStart>`, e.g. `ip#203.0.113.7#2026-10-17T09:31Z` or `user#<sub>#2026-10-17T09:31Z`.
- **Attributes:** `count`, and the TTL attribute `expiresAt` (window end plus 60 seconds, in epoch seconds; TTL deletes lazily, which doesn't matter because the window is part of the key). The partition key attribute is `id`, as in the other tables.
- **Settings:** on-demand, no backups, destroyed locally.

**Limits** (defaults per environment):

| Limit | Value per 60 s |
|---|---|
| Per IP | 300 (generous: many users can share one corporate NAT or proxy IP) |
| Per user, `USER` | 60 |
| Per user, `ORDER_MANAGER` | 300 |
| Per user, `ADMIN` | 300 |

A person with several roles gets the highest tier (roles combine, [ADR 0001](0001-role-based-access-control.md)). One budget covers all routes.

**A caller without a recognised role gets the lowest tier** (today `USER`'s 60). They hold a valid token, so they reach the API function and must be limited per user like everyone else, even though every order endpoint answers them `403`. The lowest tier needs no setting of its own, and having no role is never worth more than having one. A dedicated, stricter tier was considered: it would be another value to configure for callers the IP limit and the `403` already contain.

**Configuration, changeable at runtime.**
- **Storage:** limits live in **one JSON SSM parameter per environment** (`/orders-api/<env>/rate-limits`), created by CDK with the defaults above.
- **Format:** `{"perIp": 300, "perUser": {"USER": 60, "ORDER_MANAGER": 300, "ADMIN": 300}}`, requests per 60-second window. Every role needs a value; each value is an integer from 1 to 1,000,000.
- **Reading:** the API function reads it through a small *rate-limit config source* interface (Powertools Parameters) and caches it per Lambda instance, so operational changes take effect within the cache time, with no code change or deploy. The cache time is an environment setting (`rateLimitConfigCacheSeconds`): **2 seconds locally and in `aws-dev`**, where test suites switch limits at runtime, and **60 seconds in production** (the default) ([ADR 0010](0010-infrastructure-and-environments.md)).
- **Validation:** every value is validated against a JSON Schema. An invalid value is rejected and logged once per Lambda instance (`rate_limit_config_invalid`, at `ERROR`, with the reason but not the value), and the **last known good** configuration (or the defaults) stays in force. The raw value is what is cached, so an invalid one isn't fetched again on every request either.
- **Ownership:** **CDK owns the initial value and operations own the current one.** A redeploy only overwrites an operator's value when the default in code changes, which is then a deliberate reset.
- **Switching:** `make limits PRESET=defaults|high|tiny|tiny-ip` writes one of the named presets, which are defined in one place next to the test helpers (`defaults` is the table above; `high` is for test suites; `tiny` refuses a `USER` after 3 requests; `tiny-ip` refuses the IP after 3, below every user's limit).

**Client IP.**
- **Source:** the per-IP key is API Gateway's `requestContext.identity.sourceIp`. `X-Forwarded-For` is never honoured: clients can set it freely, and Floci passes it through untouched.
- **IPv4** is counted per address; **IPv6 per `/64` prefix**, because one host or user typically controls a whole `/64`. An IPv4-mapped IPv6 address counts as the IPv4 address it carries.
- **A request without a `sourceIp`** can only come from a misconfigured gateway, so it fails closed (`500`, logged as `source_ip_missing`), like a missing authorizer context ([ADR 0009](0009-code-structure.md)).

**Responses.**
- **Limit exceeded:** `429 Too Many Requests` with `Retry-After` (seconds until the window resets, rounded up) and the standard error body, which names the scope that was hit (`ip` or `user`).
- **Every rate-limited response** carries `RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset` (seconds), describing the more restrictive of the two limits: the one with fewer requests left, and on a tie the smaller limit. On a `429` they describe the limit that was hit.
  - "Every rate-limited response" means everything the API function answers after counting, errors included (`400`, `403`, `404`, `409`, …). Responses produced before counting (`/health`, a missing authorizer context, a counter-store error) and by the gateway itself carry none.

**When the counter store doesn't answer: fail open, with a fallback.**
- **Where it plugs in:** the limiter makes exactly one call per check through its counter-store port (`RateLimitCounterStore.increment`: add one below a limit, or say the limit is reached). The behaviour below is a store that wraps the DynamoDB store behind the same port (`FailOpenCounterStore`), so the limiter and the handler don't change.
- **Time budget:** each counter call gets about **200 ms**.
- **Throttling on a counter key** (`ProvisionedThroughputExceededException` / `ThrottlingException`) is treated as **limit exceeded → `429`**. It almost always means one client is hammering its own key, and letting it through would reward the abuse.
- **Timeouts, network and service errors** → the request is allowed. The check falls back to an **in-memory fixed window per Lambda instance** (same limits, a bounded number of keys), and a structured `WARNING` log line with the fixed event name `rate_limiter_unavailable` is written (`rate_limiter_circuit_open` when the breaker opens). In production, a CloudWatch metric filter plus an alarm on those lines makes them alertable without code changes ([ADR 0012](0012-observability-and-security-baseline.md)).
- **Circuit breaker:** after a few consecutive failures the store is skipped for about 30 seconds and only the fallback is used, so requests don't each pay the timeout during an outage. The store is retried automatically afterwards.

Refined when it was built:
- **Where each part lives:**
  - the port names the two failures a store may report, `CounterStoreThrottled` and `CounterStoreUnavailable` (`application/rate_limiting.py`); any other error is a defect
  - the policy, with its thresholds as constants, is application code without AWS imports (`application/fail_open_counters.py`)
  - translating botocore's errors and the client's timeouts belong to the DynamoDB adapter (`adapters/outbound/dynamodb_rate_limits.py`)
  - the log lines are written by the API function's adapter (`adapters/inbound/rate_limiting.py`), through an injected callback, as for the JWKS cache
- **The 200 ms per call:** the counter client makes **one attempt, no retries, with 100 ms to connect and 100 ms to read**. A retry wouldn't fit the budget, and the fallback takes its place. A count normally takes a few milliseconds.
- **The budget holds per request, too.** A request makes two checks, but after any failed call the store **rests for 1 second**, so a request whose IP check failed takes its user check from the fallback at once. A request on a failing store waits for one failed call at most, about 200 ms, and none once the circuit is open. The bound is for a store that fails: a call that is answered, however slowly, starts no rest, so a slow answer to the IP check followed by a timeout on the user check adds up (up to about 350 ms). The timeouts also apply per connect and per read, so a cold TLS handshake is bounded step by step rather than as one call.
- **What counts as an outage:** timeouts and connection errors, service errors (`5xx`), and `RequestLimitExceeded` (throughput refused for the whole account, not one key). **Other errors are our own defects** (a missing permission or table, a malformed request): they propagate as a `500`, so a deploy or test catches them instead of the limits quietly becoming per-instance ones.
- **The breaker:** **3 consecutive failed calls** open the circuit for **30 seconds**. A throttled answer is an answer, so it resets the count. When the 30 seconds are over, the next call is a **probe**: an answer closes the circuit (logged once as `rate_limiter_circuit_closed`, at `INFO`), and a failure keeps it open for another 30 seconds without logging another `rate_limiter_circuit_open`, so an alarm on openings counts outages, not probes. Lambda runs one request at a time per instance, so the probe needs no locking.
- **The fallback** counts with the same keys, so each window starts from zero as in the table. It keeps at most **10,000 counters** per instance, about 1.6 MB, and drops the **least recently used** first. A refused request is a use too, so a client hammering at its limit keeps its full counter; an idle one is the first to go. An old window's counters are never used again, so they are evicted first and need no expiry of their own.
- **Log lines:** one `rate_limiter_unavailable` per failed call (`errorType`, `consecutiveFailures`), so at most about one per second per instance until the circuit opens, then one per probe every 30 seconds. Requests counted by the fallback log nothing more. No line carries a counter key, since keys hold the client's full address.

**Edge layer: AWS WAF.**
- **What:** a regional web ACL on the REST API stage (also for the edge-optimized endpoint), defined in CDK.
- **The rule:** a rate-based rule aggregating by **`IP`**, at **3,000 requests per 5 minutes**. That is deliberately looser than the app's per-IP budget (300/min, i.e. 1,500 per 5 minutes), so WAF only catches genuine floods and the app stays the precise layer.
- **Blocked requests** get a custom response: `429` with the standard error body and a static `Retry-After`.
- **No forwarded-IP key:** WAF never aggregates on a forwarded-IP header.

Refined when it was built:
- **Where it lives:** a `WebAcl` construct inside `Api` (`infra/orders_api_infra/waf.py`). The web ACL is `orders-api-<env>`; a `WebACLAssociation` ties it to the stage; it allows everything its rules don't block.
- **The limit is an environment setting,** `wafRateLimit` (3,000 by default and in `aws-prod`). `aws-dev` sets **10,000**: one CI runner sends every suite's requests (about 3,000 within a few minutes) from one address, and the flood rule must never decide a test.
- **The deliberately low limit for testing is a second, test-only rule** (`wafTestRuleLimit`, 100 in `aws-dev`, allowed only with `testFixtures`, [ADR 0011](0011-testing-strategy.md)). It is the same rate-based rule with a scope-down statement: it counts and blocks only requests carrying the header `x-orders-api-waf-test: count-me`, so the `aws_only` test trips it while the other suites from the same address are never counted. It answers exactly like the flood rule. Anyone may send the header, and they only get their own address blocked sooner.
- **The custom response:**
  - `429`, with the `rate-limited` Problem Details from the same code as the function's (`problem_body`), with a `detail` but **without `requestId`**: WAF can't insert API Gateway's request id, and no `X-Request-Id` header is sent either
  - **`Content-Type: application/json`, not `application/problem+json`.** A WAF custom body can only be `text/plain`, `text/html` or `application/json`, and WAF refuses `Content-Type` as a custom header. The contract's `429` says so in its description, and the `aws_only` test checks the body against the `Problem` schema itself.
  - `Retry-After: 300`, the evaluation window. An address that pauses that long has nothing left in the window, whatever the limit.
  - the security headers of [ADR 0012](0012-observability-and-security-baseline.md), from the same constant the function and the Gateway Responses use
- **Visibility:** WAF's own CloudWatch metrics are on, for the web ACL and each rule. **Sampled requests are off:** WAF would keep each sample's full client address and headers, `Authorization` and so the bearer token included, for about three hours, which ADR 0012 rules out. The metrics say how much is blocked; the access logs say from where. No WAF logging configuration is built: no custom metrics, alarms or log pipelines (ADR 0012).
- **On Floci 2.3.0** CloudFormation creates the web ACL and the association. `tests/adapter/test_waf_web_acl.py` reads them back through the WAF API: rules, limits, `IP` key, window, custom response and body, and the stage association.

Considered while building it:
- **One low-limit rule in `aws-dev`:** the integration and contract suites would trip it themselves, since they share the runner's address.
- **A separate web ACL or stage for the test:** another association to keep, and it would no longer test the stage clients use.
- **Rewriting `Content-Type` to `application/problem+json`** (a CloudFront function or Lambda@Edge): needs a CloudFront distribution of our own, which the edge-optimized endpoint doesn't give us. Revisit with the own-CDN step in the Consequences.

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
  - *Failing open on every error*, defects included: the API would stay up through a broken deploy, but a missing permission would quietly turn the limits into per-instance ones, and no test would notice.
  - *Retries within the budget* (e.g. two attempts of 50 ms each): too little time per attempt for a cold connection, and the fallback already answers.
  - *A hard deadline per request* (a worker thread, or the remaining time passed down): exact, but threads and cancellation for a bound that the client timeouts and the rest after a failure already give.
  - *Splitting the budget between the two checks* (100 ms per call): too tight for a cold connection on AWS, so cold starts would fall back needlessly.
  - *Reopening the circuit only after another 3 failures* instead of one probe: each instance would pay three timeouts every 30 seconds while the outage lasts.
  - *Evicting the oldest counter first* instead of the least recently used: under churn, the client that has been hammering longest would lose its full counter first.
- **Response headers.**
  - *None:* clients can't see their budget before hitting `429`.
  - *The current IETF draft structured fields* (`RateLimit-Policy`, `RateLimit`): more precise and multi-policy, but still a draft and less familiar. This is where the headers should move once the standard settles.
- **Per-route limits** (e.g. stricter for `POST`): a useful extension, not worth a second configuration dimension now.
- **Honouring `X-Forwarded-For` / WAF's forwarded-IP key:** only correct behind a proxy we operate. Otherwise trivially spoofed.

## Consequences

- **Floods of invalid tokens are not limited locally.** On Floci, WAF is deployed and validated but **not enforced** (Floci emulates WAF's management API only). On AWS, the same CDK stack enforces it.
- **Boundary burst:** a client can send up to twice its limit in a short burst across a window boundary. This is the documented trade-off of fixed windows.
- **Cost and latency:** two DynamoDB writes per request, **$1.25 per million requests** (2 × $0.625 per million on-demand write request units, `us-east-1` list price on 2026-10-17), about a third of the REST API's own $3.50 per million and the largest DynamoDB item in the cost model ([design document, section 8](../design.md#cost-model); [ADR 0013](0013-production-readiness-and-evolution.md)), plus about 10–20 ms of added latency. *Corrected with the cost model:* this line first said "on the order of the REST API's own price", written before DynamoDB's on-demand price cut. **Hot keys:** a single abusive IP concentrates on one partition key (about 1,000 writes/s, and rejected writes still consume capacity). WAF absorbs that in production.
- **Limits are approximate during a counter-store outage:** with the in-memory fallback, the effective limit is the configured limit times the number of warm Lambda instances.
  - A single failed call moves about a second of one instance's traffic to the fallback, and an instance with more than 10,000 distinct clients in a window forgets the idlest ones' counts. Both only make the limits more lenient, briefly, never stricter.
  - The 200 ms bound has two gaps botocore's timeouts don't cover: **resolving the endpoint's DNS name**, and a server that keeps sending a byte now and then, since the read timeout applies per read. DynamoDB's answers are a few hundred bytes, and Lambda's resolver is local, so neither is expected; the per-request bound would need a hard deadline if they ever show up.
- **To verify on the first real AWS deploy** (the `aws_only` tests, [ADR 0011](0011-testing-strategy.md)):
  - that WAF's `IP` key and `sourceIp` both see the client's address for the edge-optimized endpoint. This is stated in an AWS re:Post expert article, but not clearly in the official documentation.
  - that WAF really blocks, with the custom response as configured
  - how WAF aggregates IPv6 addresses. The app counts IPv6 per `/64`. The tests run over IPv4 only, so this stays to be checked by hand.
- **The edge's `429` differs from the function's** in its content type (`application/json`), and it has no request id. A client that reads the body as Problem Details by its fields, not by its media type, handles both.
- **A CDN of our own in front later** (regional endpoint plus our own CloudFront): WAF moves to CloudFront (scope `CLOUDFRONT`), and the app's per-IP key switches to the client address appended by that trusted proxy (a configured number of hops counted from the right, never the leftmost value).
- **Local tests** all come from one Docker IP, so per-IP behaviour is covered mainly by unit tests, and the per-IP integration test uses its own configuration.
- **A parameter that can't be read at all** (deleted, or SSM unreachable once the cache has expired) is treated like an invalid value: the last known good limits (or the defaults) stay in force, a `WARNING` `rate_limit_config_unavailable` is logged with the error type, and SSM is not asked again until the cache time has passed, so requests don't each pay for the failed read. A missing configuration never takes the API down.
