---
status: accepted
---

# Validation at the gateway from one OpenAPI schema, and RFC 9457 Problem Details for every error

Request bodies are validated **at API Gateway** by request validators, so an invalid body never invokes the API Lambda function. The **same JSON Schema**, taken from the OpenAPI document (the single source of truth), is enforced again inside the function. Everything the gateway can't check (query parameters, path format, content type, business rules) is checked in code before any data access. **Every error any layer returns, whether WAF, API Gateway or our code, is an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) Problem Details document**, so clients handle one format whichever layer rejected them.

## Decision

**Error format.** `Content-Type: application/problem+json`:
```json
{ "type": "urn:problem:orders-api:validation-error",
  "title": "Request validation failed",
  "status": 400,
  "detail": "Invalid request body: $.items[0].quantity: must have a maximum value of 999",
  "instance": "/api/v1/orders",
  "requestId": "c0ffee-…",
  "errors": [ { "pointer": "/items/0/quantity", "detail": "must be at most 999" } ] }
```
- **`type`** is a stable URN identifying the error kind. It is the machine-readable code and is documented in OpenAPI.
- **`requestId`** is API Gateway's request id (`$context.requestId`). It is in every error body, in an `X-Request-Id` header on every response, and in every log line.
- **`errors[]`** appears only when code produced field-level errors. Each entry has a message plus either `pointer` (a JSON Pointer into the body) or `parameter` (the name of a path or query parameter). Gateway validation yields a single message, which goes into `detail`.
- **`scope`** (`ip` or `user`) appears on `429` ([ADR 0006](0006-rate-limiting.md)).

| `type` (`urn:problem:orders-api:…`) | Status |
|---|---|
| `validation-error` | 400 |
| `unauthorized` | 401 |
| `forbidden` | 403 |
| `not-found` | 404 |
| `conflict` | 409 |
| `precondition-failed` | 412 |
| `payload-too-large` | 413 |
| `unsupported-media-type` | 415 |
| `idempotency-key-mismatch` | 422 |
| `rate-limited` | 429 |
| `internal-error` | 500 |
| `bad-gateway` | 502 |
| `service-unavailable` | 503 |
| `gateway-timeout` | 504 |

**Where each check lives.**

| Layer | Checks |
|---|---|
| **API Gateway request validators** | Bodies of `POST /api/v1/orders` and `PATCH /api/v1/orders/{id}/status` against their JSON Schema (draft-04) models. Covers required fields, types, ranges, patterns, enums, item counts and `additionalProperties: false`. **An invalid body never invokes the API function.** |
| **API function, before any data access** | `Content-Type`; body size; query parameters; path `{id}` format; the same body schema again (see below); duplicate SKUs |
| **API function, domain logic** | Status changes, ownership, idempotency and `If-Match` ([ADR 0001](0001-role-based-access-control.md), [ADR 0004](0004-order-model-and-lifecycle.md)) |

**One schema, two enforcers.**
- **Source:** the OpenAPI document holds the request schemas, and the gateway models are generated from it.
- **In the function:** **Powertools Validation** (`fastjsonschema`) checks bodies against **the identical schema**, so the gateway and the function can't drift apart and a client gets the same `400` from either.
- **Typing:** **Powertools Parser** then turns the validated body into Pydantic models that carry **types only, no constraints**, leaving nothing to keep in sync.
- **Why the function checks again:** for defence in depth, for unit tests that run without a gateway, and for the container exit in [ADR 0002](0002-serverless-rest-api-single-lambda.md), where there is no gateway.

**Field rules.**

| Field | Rule |
|---|---|
| `currency` | enum `EUR`, `USD`, `GBP` |
| `items` | 1–50 entries |
| `items[].sku` | `^[A-Z0-9][A-Z0-9-]{1,31}$` |
| `items[].quantity` | integer 1–999 |
| `items[].unitPrice` | integer minor units 0–1,000,000 |
| `PATCH` `status` | enum of the five statuses |
| every object | `additionalProperties: false` |

- **Status values:** a valid status that is not an allowed change is `409` ([ADR 0004](0004-order-model-and-lifecycle.md)), not `400`.
- **Duplicate SKUs:** rejected in code with `400`, with `errors[]` pointing at the duplicate. JSON Schema's `uniqueItems` compares whole objects, so it can't express this.

**Edge cases.**

| Case | Response | Layer |
|---|---|---|
| Body fails the schema, including unknown fields such as `ownerId` or `totalAmount` | `400` | gateway (and function) |
| Body isn't JSON, or is empty on `POST`/`PATCH` | `400` | gateway / function |
| `Content-Type` isn't `application/json` on `POST`/`PATCH` | `415` | function |
| Body over 64 KB | `413` | function |
| Unknown query parameter; `limit` not an integer 1–100; cursor doesn't decode | `400` with `errors[]` | function |
| Path `{id}` doesn't match the UUID pattern declared in the OpenAPI document | `400` with `errors[]` pointing at the path parameter | function (the gateway only checks that parameters are present, not their format) |
| Unknown path or method | `404` | gateway (`MISSING_AUTHENTICATION_TOKEN` remapped from its default `403`) |
| `Accept` asks for something other than JSON | ignored; responses are always JSON | — |

**Unexpected errors and gateway defaults.**
- **Uncaught exceptions:** a top-level handler in the function turns them into `500 internal-error` with a generic `detail`. No stack trace or internals go into the response; the full stack trace is logged with the request id.
- **Gateway Responses mapped to Problem Details:**
  - `BAD_REQUEST_BODY` / `BAD_REQUEST_PARAMETERS` → `400`, with `detail: $context.error.validationErrorString`. The message can contain quotes (e.g. an enum's allowed values, or a client-chosen field name). Floci inserts it raw but evaluates VTL, so on Floci the template escapes it; AWS documents these templates as plain substitution, so on AWS it is inserted as is. The template is therefore chosen per target, and the AWS form is verified on the first AWS run.
  - `UNAUTHORIZED` → `401` ([ADR 0003](0003-access-token-validation.md))
  - `AUTHORIZER_FAILURE` → `503`
  - `MISSING_AUTHENTICATION_TOKEN` → `404`
  - `INTEGRATION_FAILURE` → `502`
  - `INTEGRATION_TIMEOUT` → `504`
  - `DEFAULT_4XX` / `DEFAULT_5XX` → generic Problem Details, so nothing ever returns the gateway's own `{"message": …}` shape
- **WAF's custom `429`** uses the same format (ADR 0006).
- **Timeouts:** the API function times out at **10 s** and the authorizer at **5 s**, both below the REST API's 29 s integration limit, so our own timeout fires first and is logged.

## Considered options

- **A custom error envelope** (`{"error": {"code", "message", "details"}}`). Simple and common, but non-standard, so every client must learn it from our docs. *Rejected* in favour of the IETF standard, which also renders statically in gateway and WAF templates.
- **Validation in code only.** Simplest and gives rich field errors everywhere, but every invalid body invokes the API function. *Rejected*, because keeping invalid traffic away from the function is the point.
- **Gateway validation only.** The least code. *Rejected*: unit tests couldn't cover body validation, and the container exit would lose all validation.
- **Pydantic constraints in the function, kept in sync with the OpenAPI schema by a test.** Gives rich multi-field errors. *Rejected*: two places hold the rules, the test has to guard their equality, and Pydantic emits schemas in draft 2020-12 while the gateway needs draft-04. Worth revisiting if rich field errors ever matter more than a single schema.
- **Powertools Event Handler validation** (`enable_validation=True`). It generates OpenAPI *from* the code, the opposite of the OpenAPI document being the source. Its router remains a candidate for the code structure.
- **`422` for semantic validation errors.** The gateway answers `400` for schema errors anyway, so `422` elsewhere would make the same mistake produce two codes depending on which layer caught it. `422` is reserved for the idempotency-key mismatch.
- **`405` with an `Allow` header for a wrong method.** The gateway can't tell a wrong method from an unknown path without extra mock routes. `404` is used instead, and `405` is a possible refinement.
- **`404` for a malformed path id** (making it look the same as an unknown order). Considered first, but the id format is part of the published contract, so breaking it is invalid input (`400`). It leaks nothing, because the format is public, and it matches what contract tests expect for values that break a declared pattern.
- **Dereferenceable `type` URIs** (`https://…/problems/<slug>`). RFC 9457 prefers them, but there is no docs site to point at. URNs are stable and don't pretend to be pages. Switch once docs are hosted.
- **Merging duplicate SKUs silently.** Surprising for clients. *Rejected.*

## Consequences

- **Gateway-rejected requests carry no `errors[]`.** API Gateway reports one message string, so field-level detail for invalid bodies is a single message in `detail`. Only code-detected errors list every field.
- **The gateway models depend on draft-04.** Request schemas in the OpenAPI document must stay within what JSON Schema draft-04 (API Gateway models) and `fastjsonschema` both support. How the OpenAPI document is written and turned into gateway models is decided with the API contract.
- **To verify on Floci and on the first AWS deploy:**
  - whether the authorizer runs before request validation (the usual understanding, not clearly documented). If it does, an invalid body from an authenticated caller can still cost an authorizer call on a cache miss, but never an API function call.
  - how the gateway treats a non-JSON `Content-Type` on a validated method.
  - that the remapped Gateway Responses render as configured.
- **Every row of the edge-case table gets an integration test** against the stack on Floci. The function-layer rows also get unit tests.
