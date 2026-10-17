---
status: accepted
---

# Production readiness and evolution: deliberate simplifications, measurable triggers, single-region DR

This take-home makes **deliberate simplifications**. This ADR is the single list of them and says **what signal would change each decision**. It is the source for the design doc's "Production evolution" section. Each item links to the ADR whose decision it extends, and the detail stays there.

Nothing here is built. It is grouped **by trigger**, not by area: a flat backlog would say what *could* be done, not *when* or *why*.

## Decision

### 1. Before go-live: simplifications that are fine for the take-home, not for real users

Ordered by risk (correctness and security first, then operability, then hardening):

| # | Item | Today | Extends |
|---|---|---|---|
| 1 | **Server-side pricing** from a catalogue or pricing service. A client price is at most checked against it | Clients set unit prices | [0004](0004-order-model-and-lifecycle.md) |
| 2 | **Hosted login with authorization code + PKCE** (or SRP), plus an MFA policy | Password sign-in, for the demo and tests | [0010](0010-infrastructure-and-environments.md) |
| 3 | **Run the `aws_only` checks on the target account:** WAF and `sourceIp` see the client behind the edge-optimized endpoint; WAF's IPv6 aggregation; API Gateway accepts the OpenAPI 3.0.3 import | Floci can't show these | [0006](0006-rate-limiting.md), [0007](0007-validation-and-errors.md), [0008](0008-openapi-contract.md), [0011](0011-testing-strategy.md) |
| 4 | **Minimum alerting:** metric filter + alarm on `rate_limiter_unavailable` / `rate_limiter_circuit_open`; Lambda errors and throttles; API Gateway 5XX and latency; DynamoDB throttles | Logs only | [0012](0012-observability-and-security-baseline.md) |
| 5 | **Deployment pipeline with approvals,** and a separate production account (multi-account setup) | Manual deploys; `aws-e2e` is a verification tool, already on GitHub OIDC | 0010, 0011 |
| 6 | **Custom domain + ACM certificate** (in `us-east-1` for the edge-optimized endpoint, whatever region the API runs in) | API Gateway's default domain | [0002](0002-serverless-rest-api-single-lambda.md) |
| 7 | **WAF managed rule sets** (common rules, known bad inputs, IP reputation) next to the rate rule, and WAF logging | Rate rule only; WAF's metrics and sampled requests | 0006 |
| 8 | **HMAC-signed cursors** | Encoded, unsigned. Safe, because queries are scoped to the caller, but tamperable within that scope | 0004 |
| 9 | **A tested restore.** PITR is on in production, but a backup is only real once a restore has been rehearsed | — | [0005](0005-dynamodb-storage.md) |
| 10 | **Revocation runbook:** lock a user out now (global sign-out, disable the user) and the documented window (access-token lifetime + 5-minute authorizer cache) | — | [0001](0001-role-based-access-control.md), [0003](0003-access-token-validation.md) |
| 11 | **Erasure of everything belonging to one person** (GDPR right to erasure). Not covered today: deleting all of a `sub`'s orders and idempotency records, and handling their traces in logs within the retention period | Only per-order hard delete exists | new |

### 2. When load grows: each step has a measurable trigger

| Trigger | Step | Extends |
|---|---|---|
| `byCreated` writes approach ~1,000/s (one GSI partition) | Write sharding (`ORDER#<0..N-1>`), merging the shards when listing | 0005 |
| Sustained traffic in the tens of millions of requests per month, where the cost model shows the crossover | Containers (ECS/Fargate) behind a new inbound adapter; the core is unchanged | 0002, [0009](0009-code-structure.md) |
| Rate-limit counters dominate latency or cost per request, or a single abusive key hits partition limits | Counters in ElastiCache Serverless (Valkey/Redis), with Lambda in the VPC | 0006 |
| Cold starts break latency targets | Lambda SnapStart for Python, or provisioned concurrency; split, slimmer authorizer bundle | 0002, 0009 |
| Steady, predictable load | Provisioned capacity with auto-scaling instead of on-demand | 0005 |
| Routes diverge in permissions, scaling or ownership | Split the single API function (per route or read/write) | 0002 |
| The 2× burst at window boundaries matters | Sliding-window counter (or token bucket) | 0006 |

### 3. When requirements change: each step is tied to a new requirement

| Requirement | Step | Extends |
|---|---|---|
| Instant revocation or role changes | Role table read per request, or token introspection; the authorizer cache then pays off more | 0001, 0003 |
| Several identity providers, or migrating providers | Internal user id mapped from (issuer, `sub`), or a one-off subject migration | 0001 |
| Client applications must be restricted | Check OAuth scopes in addition to roles | 0003 |
| Audit trail | DynamoDB Streams → S3 (or an audit store), recording every change including deletes | 0004, 0005 |
| Reporting or ad-hoc queries | Analytics export (S3 + Athena), or more GSIs for specific patterns | 0005 |
| Filters or sorting on lists | One GSI per filter, added before data grows (a destroy and redeploy on Floci) | 0004, 0005 |
| Safe, gradual limit changes | AWS AppConfig (validators, gradual rollout, automatic rollback) as a new config-source adapter | 0006 |
| Per-route limits | A second configuration dimension in the rate limiter | 0006 |
| Our own CDN or caching in front | Regional endpoint + own CloudFront; WAF moves to CloudFront; the app uses the trusted-proxy hop for the client IP | 0002, 0006 |
| Observability beyond logs | Powertools Metrics (EMF) with the cardinality rule (never `sub`, order id, IP or request id as a dimension), X-Ray tracing, dashboards | 0012 |
| Browser clients | CORS, and "Try it out" in the docs | 0008 |
| Hosted documentation | Published OpenAPI docs; dereferenceable problem `type` URIs; OpenAPI 3.1 once API Gateway imports it | 0007, 0008 |
| Several teams or deploy cadences | Split stacks by lifecycle, passing values through SSM parameters instead of exports | 0010 |
| A higher availability requirement | Warm standby (see Disaster recovery) | this ADR |

### Disaster recovery

**Go-live posture: single region with backups.** The targets are stated as explicit assumptions:
- **Table corruption or bad writes:** restore from PITR (any second in the last 35 days). RPO is **minutes**; RTO is **hours** (restore to a new table, then redeploy with CDK pointing at it).
- **Region outage:** the service is unavailable until the region recovers. No data written before the outage is lost.

**Evolution: warm standby** in a second region (Global Tables, an idle stack, Route 53 failover), triggered by an availability requirement. Its two real constraints:
1. **Identity is regional.** Cognito user pools are not replicated, and passwords can't be exported. Region B needs its own pool plus a user migration or password reset, or a globally available identity provider. ADR 0001's swappable provider is what makes the latter possible.
2. **Replication semantics.** Global Tables replicate with last-writer-wins by default. With writes in two regions, concurrent status changes could silently overwrite each other, breaking ADR 0004's lifecycle. Warm standby avoids this by writing in **one** region at a time.

**Active-active is ruled out** for this lifecycle unless strongly consistent multi-region replication is used. Its current constraints (latency, supported regions) are to be checked against AWS documentation before any such decision.

### Cost model

The design doc includes **one table: monthly cost in the AWS environments' region at 1 M, 10 M and 100 M requests per month**.
- **Line items:** API Gateway (REST); Lambda (arm64, both functions, with an assumed authorizer cache-hit rate); DynamoDB (order reads and writes plus two rate-limit writes per request); WAF (web ACL, rules, requests); CloudWatch Logs ingestion; Secrets Manager; Cognito monthly active users.
- **Assumptions are stated:** request mix, memory, duration, log bytes per request.
- **Prices come from the AWS pricing pages at the time of writing**, cited with the date.
- **The table must show** which items dominate and roughly where the container crossover sits.
- **Corrections:** where current prices contradict a figure in an earlier ADR (e.g. ADR 0006's counter cost per million requests, written from memory before a known DynamoDB price change), that ADR is corrected.

Refined when the table was written ([design document, section 8](../design.md#cost-model)), and re-priced on 2026-10-18 when the AWS environments moved to `eu-central-1` ([ADR 0010](0010-infrastructure-and-environments.md)):
- **The table is per month in `eu-central-1` at list prices,** read from AWS's Price List API files for the region, with the pricing pages for the price structure, and every source and its date in the design document. One region, one table: `us-east-1` is 6–11 % cheaper on the request path, which the design document notes in one line.
- **Corrected:** ADR 0006's counter cost. The two writes cost $1.53 per million requests, about 40 % of the REST API's $3.70, not "on the order of" it. ADR 0002's API Gateway prices are Frankfurt's: $3.70 per million for REST, $1.20 for HTTP APIs, both from the same Price List file. Its Fargate figure was not part of the research and stays an estimate.
- **Secrets Manager costs nothing in `aws-prod`,** which has no demo users and so no secret. `aws-dev` pays $0.40 a month for its one secret, pro rata.
- **What dominates:** the request path costs about $8.30 per million requests, plus WAF's $6 a month base. **Cognito is billed per monthly active user, and becomes the largest item from about 10 M requests a month** (at 200 requests per user). The stack therefore pins every environment's user pool to the **Lite** tier: the design uses none of the Essentials features (groups already arrive in the access token; no managed login, no token customization), and Essentials, CloudFormation's default, costs 2.5–3× Lite at that scale. `eu-central-1` offers Lite at the same prices as `us-east-1`. Managed login (go-live item 2) or token customization would need Essentials; that is a tier change, not a redesign.
- **The container crossover:** API Gateway and Lambda together cost about $4.10 per million requests. That is what a container setup would replace with its fixed cost, so each $100 a month of containers breaks even at about 24 M requests a month.

## Considered options

- **Grouping by area** (security, data, scale, operations). Easy to scan, but silent on *what first* and *why*.
- **A phased roadmap with timelines.** It implies a schedule that can't be justified without real traffic or a real organisation.
- **Repeating each ADR's evolution text here.** Rejected: each decision lives in one place, so this ADR lists and links.
- **Disaster recovery:**
  - *Warm standby from day one:* double the infrastructure, plus a regional identity problem, for an availability requirement nobody has stated.
  - *Active-active:* unsafe for the conditional-write lifecycle under last-writer-wins replication.
- **No cost model.** The cost claims in ADRs 0002, 0005 and 0006 would stay unbacked.
- **A spreadsheet calculator.** More than a take-home needs.

## Consequences

- **The design doc's "Production evolution" section** is a short introduction plus the three tables, the DR posture and the cost table, linking here and into the ADRs.
- **Writing the cost table needs a pricing research step**, with dated sources, at the time the doc is written.
- **Go-live item 11 (erasure)** is a known functional gap, named openly rather than discovered by a reviewer.
