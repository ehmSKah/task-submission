---
status: accepted
---

# Serverless compute: API Gateway REST API (v1) in front of a single Lambda function

The Orders API runs on **AWS Lambda behind an API Gateway REST API (v1)**. **One Lambda function serves every route**; API Gateway declares each of the six routes explicitly and points them all at that function, which dispatches internally. The workload, an orders API with modest traffic and bursts, fits pay-per-request scaling to zero with no servers to operate. It is also the model the local Floci stack runs end to end through CDK, so the local deployment is the real topology rather than a stand-in.

## Decision

- **Compute:** AWS Lambda (Python).
- **Front door:** API Gateway **REST API (v1)** with Lambda proxy integration, **edge-optimized endpoint**. Clients reach the API through an AWS-managed CloudFront distribution, which shortens the network path for clients spread across regions.
- **Routes:** declared explicitly in API Gateway (`POST /api/v1/orders`, `GET /api/v1/orders`, `GET /api/v1/orders/{id}`, `PATCH /api/v1/orders/{id}/status`, `DELETE /api/v1/orders/{id}`, `GET /api/v1/health`). Unknown paths are rejected by the gateway and never reach the function. No catch-all `{proxy+}`.
- **Functions:** a single API function ("lambdalith") with an internal router. Token validation runs in a separate Lambda authorizer in front of it ([ADR 0003](0003-access-token-validation.md)).
- **Health:** `GET /api/v1/health` reaches the same function without an authorizer and is a **shallow** check. It reports that the function runs and does not call DynamoDB, so a database incident doesn't also fail the health check, and frequent probing costs nothing.

## Considered options

**Compute: container (FastAPI on ECS Fargate behind an ALB).**
- *For:* no cold starts; cheaper per request under sustained high load; runs anywhere (Kubernetes, other clouds, on-premises).
- *Against:* an always-on minimum cost (roughly $30–50 a month for one task plus the ALB) before any traffic. We would own base-image patching, scaling policies, health checks and VPC/ALB setup. Locally, Floci would only provide data and identity, while the compute ran outside the CDK-deployed stack.
- *Verdict:* rejected for this workload. It becomes the right choice if traffic turns high and steady (see Consequences).

**API Gateway flavour: HTTP API (v2).**
- *For:* about $1.00 per million requests vs about $3.50 for v1, with slightly lower latency. Its native JWT authorizer works with any OIDC provider.
- *Against:* no direct AWS WAF attachment (needs CloudFront in front), no request validation, no API keys or usage plans, no response caching.
- *On Floci:* the JWT authorizer rejects Floci's own tokens without extra emulator flags. The Lambda authorizer ignores result caching and answers `500` instead of `401`. With payload 2.0, `sourceIp` is always `127.0.0.1`.
- *Verdict:* rejected. WAF integration and faithful local behaviour (authorizer caching, real client IPs) outweigh the price difference at this scale. Revisit v2 if cost per request matters more than WAF and gateway-side validation.

**Functions: one per route (six functions).**
- *For:* least-privilege IAM per function (read-only for `GET`s), independent memory, concurrency and alarms per route, smaller blast radius.
- *Against:* six cold-start pools, so rarely used routes are almost always cold. Setup work (JWKS cache, configuration, SDK clients) is repeated per function. More CDK and shared-code packaging. Moving to a container would mean reassembling the app.
- *Verdict:* rejected. A **read/write split** (two functions) was the middle ground and was rejected for the same reasons, at a smaller scale.

**Routing: catch-all `{proxy+}`.**
- *For:* less infrastructure code.
- *Against:* every unknown path reaches and bills the function. Per-route authorizer settings (an unauthenticated `/health`) and gateway-side validation are harder to express.
- *Verdict:* rejected.

- **Endpoint type: regional, with our own CloudFront distribution in front.** That gives full control (caching, a CloudFront-scoped WAF, Shield Advanced), but it is one more component to run, and the per-IP keys would have to use a trusted forwarded address. *Rejected* in favour of the managed edge-optimized endpoint; it is the path to take if caching or edge-level WAF is needed. A plain *regional endpoint* without CloudFront suits clients in one region only.

## Consequences

- **No shared in-memory state.** Lambda instances don't share memory and are short-lived, so anything shared across requests (rate-limit counters, idempotency records) must live in an external store. Rate limiting as a whole is decided separately.
- **Cold starts.** Python with cryptography and validation libraries typically cold-starts in a few hundred milliseconds. Mitigations if latency targets demand it: Lambda SnapStart for Python, or provisioned concurrency. One shared function keeps the number of cold starts low.
- **Cost crossover.** Per-request pricing (Lambda plus REST API) overtakes an always-on container somewhere around tens of millions of requests per month at steady load. Past that point, a container on ECS/Fargate is the documented evolution path.
- **AWS lock-in, contained in code.** The Lambda handler and API Gateway event format tie this layer to AWS. The order logic and authorization rules must know nothing about Lambda or API Gateway, so a move to a container means writing a new adapter, not a rewrite. This mirrors the swappable identity provider in [ADR 0001](0001-role-based-access-control.md).
- **One IAM role for the function** holds the union of all permissions on this service's own tables. It's acceptable while the routes share one owner and one data set. Split functions (per route, or read/write) when routes need different permissions, scaling or ownership.
- **WAF is available for production.** Rate-based rules and managed rule sets attach directly to the REST API stage. The local Floci stack doesn't emulate WAF; production use is covered in the production-evolution notes.
- **Edge-optimized endpoint specifics.**
  - A custom domain needs its ACM certificate in `us-east-1`.
  - The AWS-managed CloudFront distribution can't be configured: no caching, no WAF of our own at the edge locations.
  - WAF still attaches as a *regional* web ACL to the API stage ([ADR 0006](0006-rate-limiting.md)).
- **Local Floci specifics:**
  - Floci never enforces API Gateway throttling or usage plans.
  - The REST API id can't be pinned and changes on every recreate.
  - CDK's `api.url` output is the AWS form, not the local invoke URL.
