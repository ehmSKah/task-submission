---
status: accepted
---

# Code structure: a hexagonal Python package with Powertools at the edges

The code is **one Python package** organised as **ports and adapters**. The domain and use cases are plain Python with **no AWS imports**. AWS lives only in adapters: API Gateway events in, DynamoDB and SSM out, Lambda handlers as entry points. That is what makes the promises in earlier ADRs literally true:
- the core knows nothing about Lambda ([ADR 0002](0002-serverless-rest-api-single-lambda.md)), so a move to containers is a new inbound adapter
- the token validator is standalone ([ADR 0003](0003-access-token-validation.md))
- the rate-limit configuration source is swappable ([ADR 0006](0006-rate-limiting.md))

**AWS Powertools for Lambda** covers the Lambda edge: routing, validation, parsing, idempotency, parameters and logging.

## Decision

**Architecture: hexagonal, pragmatic.**
- **Dependency direction:** adapters → application (use cases + ports) → domain. Never the other way.
- **Ports only where a second implementation is real:**
  - test fakes
  - the documented swaps: AppConfig for SSM, Redis for DynamoDB counters, a container for Lambda, another identity provider
- **Ports:** `OrderRepository`, `RateLimitCounterStore`, `RateLimitConfigSource`, `Clock`, `IdGenerator`.
- **Deep modules:** behind each port sits a module with a small interface hiding substantial behaviour. For example, `RateLimiter.check(ip, subject, roles) -> Decision` hides fixed windows, IPv6 `/64` keys, role tiers, the in-memory fallback and the circuit breaker. The OIDC validator hides discovery, JWKS caching and the nine checks.
- **Enforced:** an **import-linter** contract in CI fails the build if `domain` or `application` import `boto3`, `aws_lambda_powertools` or any adapter module.

**Layout.**
```
task1/
├── pyproject.toml            # one uv project; uv.lock committed
├── openapi/openapi.yaml      # the contract (ADR 0008)
├── src/orders_api/
│   ├── domain/               # Order, status rules, totals, permissions, rate-limit decisions
│   ├── application/          # use cases and ports
│   ├── auth/                 # OIDC validator + JWKS cache, no Lambda imports
│   ├── adapters/
│   │   ├── inbound/          # api_handler (Powertools router + middleware), authorizer_handler
│   │   └── outbound/         # DynamoDB order repository and counters, SSM config source, idempotency persistence
│   └── contract/             # loads request schemas from openapi.yaml for Powertools Validation
├── infra/                    # CDK app (Python)
└── tests/  unit/  integration/  contract/
```

**Inside the API function.**
- **Router:** Powertools **Event Handler** (`APIGatewayRestResolver`), used as a **router only**. Its own validation is off, because the OpenAPI document is the source (ADR 0007/0008). Exception handlers turn domain and adapter errors into Problem Details.
- **Body validation:** **Powertools Validation** against the OpenAPI request schemas, then **Powertools Parser** into Pydantic models that carry types only.
- **Idempotency:** **Powertools Idempotency** (`@idempotent_function`) wraps the call to `create_order` in the adapter layer, for `POST /api/v1/orders` only.
  - **Key:** `{owner: sub, key: Idempotency-Key}`. The header name is looked up case-insensitively first, because REST APIs pass the client's casing.
  - **Fingerprint:** payload validation hashes the parsed order, not the raw body (ADR 0004).
  - **Where it lives:** the wrapper that reads the header and replays responses is an inbound adapter; the DynamoDB persistence layer is an outbound adapter.
  - **Missing header:** `raise_on_no_idempotency_key=False`, so the request runs normally.
  - **Expiry:** 24 h. In-progress records are bounded by the Lambda's remaining time (`register_lambda_context`).
  - **What is stored:** the serialised `201` response.
  - **Errors:** a validation error maps to `422`; already-in-progress maps to `409`.
  - **Store:** the idempotency table ([ADR 0005](0005-dynamodb-storage.md)).
- **Configuration:** Powertools **Parameters** reads the SSM rate-limit configuration with a 60-second cache, behind `RateLimitConfigSource`.

**Request pipeline in the API function.** The order decides which error a client sees when several things are wrong:

| # | Step | Possible result |
|---|---|---|
| 0 | Logger context; correlation id = `requestContext.requestId` | — |
| 1 | `GET /api/v1/health` returns here (no identity, no rate limit) | `200` |
| 2 | Identity from the authorizer context (`sub`, roles). If it's missing, that's a misconfiguration: fail closed | `500` + error log |
| 3 | Rate limit: per IP, then per user | `429` |
| 4 | Request checks: `Content-Type`, body size, query parameters, **path `{id}` matches the UUID pattern** | `415` / `413` / `400` |
| 5 | Body: schema validation, duplicate SKUs, then parsing into typed models | `400` |
| 6 | Permission check ([ADR 0001](0001-role-based-access-control.md)) | `403` |
| 7 | Idempotency wrapper (`POST` only), then the use case: load and visibility, rules, conditional write | `404` / `409` / `412` / `422` |
| 8 | Response: serialise, set `X-Request-Id`, `RateLimit-*`, `ETag` | `200` / `201` / `204` |
| — | Exception handlers: known errors → Problem Details; anything else → generic `500` | |

- **Rate limiting comes before validation**, so clients sending garbage still spend their budget; malformed requests are no way around the limiter.
- **Request and body validation come before the permission check.** For `PATCH` the required permission depends on the body (cancellation needs `order:cancel:own`, anything else `order:status:any`). The gateway also rejects invalid bodies before the function, so a `400` precedes a `403` in both layers.
- **A malformed path id is a `400`**, because the UUID format is part of the published contract ([ADR 0007](0007-validation-and-errors.md)). A well-formed id that doesn't exist or isn't visible is a `404` (ADR 0001).

**Packaging.**
- **One code bundle, two functions:** the authorizer (`orders_api.adapters.inbound.authorizer_handler.handler`) and the API function (`orders_api.adapters.inbound.api_handler.handler`).
- **Dependencies are bundled in the zip, not a layer.** That gives one artefact per deploy, with versions pinned by `uv.lock`.
- **Runtime:** Python 3.13. The architecture is set explicitly for both the build and the function definition: **`arm64` on AWS, the host's architecture locally**, because Floci runs the host's architecture ([ADR 0010](0010-infrastructure-and-environments.md)).
- **`openapi.yaml` ships inside the bundle,** so the function validates against the same file the gateway was built from.

## Considered options

- **Classic layers** (handlers → services → repositories). Familiar, but nothing enforces that services stay free of AWS, so the boundary erodes.
- **Flat handler modules.** Fastest to write, but they would break the portability promises in ADRs 0002, 0003 and 0006.
- **FastAPI + Mangum.** The same app would run unchanged in a container, the strongest portability. *Rejected:* a heavier cold start, and FastAPI's code-first OpenAPI and validation would sit unused or duplicate ADRs 0007/0008. The hexagonal core already makes a container move a new adapter. It is the alternative if "the same app in a container" ever matters more.
- **A hand-written dispatch table.** The smallest option, but it reinvents exception handling and middleware that Powertools already provides.
- **Idempotency on the whole handler (`@idempotent`).** Its key can't combine the owner's `sub` with the header, and it would apply to every route.
- **A separate, slimmer bundle per function.** The authorizer only needs `auth/` and PyJWT, so it would cold-start faster, at the cost of two build pipelines. This is the optimisation if authorizer cold starts show up in latency.
- **Lambda layers** (AWS's public Powertools layer, or our own). The public layer doesn't exist on Floci, and our own layer adds versioning work and can drift from the function code.
- **`x86_64` on AWS.** CDK's default, but arm64 is cheaper per millisecond on AWS. Locally the bundle follows the host, because Floci runs the host's architecture and mismatched native wheels (`cryptography`) fail to import.

## Consequences

- **More files and indirection** than a flat design. Mitigated by allowing ports only where a second implementation exists.
- **Unit tests use in-memory fakes** for the ports and need no Floci and no mocking of boto3. Integration tests exercise the real adapters on Floci.
- **The authorizer carries the whole bundle,** so its cold start includes packages it doesn't use. That is accepted until it shows up in latency.
- **A missing authorizer context is a `500`, not a `401`.** It can only happen through misconfiguration, so it is logged as an error and fails closed.
- **Observability is logs only** (Powertools Logger). Custom metrics, tracing and alarms are production evolution ([ADR 0012](0012-observability-and-security-baseline.md)).
