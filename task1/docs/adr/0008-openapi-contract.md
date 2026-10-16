---
status: accepted
---

# The API contract: a design-first OpenAPI 3.0.3 document that also defines the gateway

The API is described **design-first** in one OpenAPI document. That document is a deliverable in its own right, and it **is** the API Gateway definition: CDK creates the REST API from it with `SpecRestApi`. The routes, request models, validators, authorizer binding and gateway responses therefore can't drift from the published contract. The published document stays free of AWS details; the `x-amazon-apigateway-*` extensions are added at synth time. Requests are enforced against the contract by the gateway and the Lambda ([ADR 0007](0007-validation-and-errors.md)). Responses are proven against it by tests.

## Decision

**The document.**
- **Location:** `task1/openapi/openapi.yaml`, **OpenAPI 3.0.3**, the newest version API Gateway imports.
- **What it describes:**
  - **Operations:** all six.
  - **Errors:** the Problem Details schema and its 14 `urn:problem:orders-api:*` types.
  - **Headers:** `X-Request-Id`, `Retry-After`, `RateLimit-Limit/-Remaining/-Reset`, `ETag` / `If-Match`, `Idempotency-Key`.
  - **Listing:** the list envelope `{ items, nextCursor }`.
  - **Security:** the bearer-token security scheme.
- **Every operation documents every status code it can return**, including `401`, `429`, `5xx` and the edge cases from ADR 0007.

**From document to gateway.**
- **At `cdk synth`**, a small build step reads the clean document and adds the API Gateway extensions:
  - `x-amazon-apigateway-integration` (Lambda proxy to the API function)
  - `x-amazon-apigateway-authorizer` (the REQUEST authorizer from [ADR 0003](0003-access-token-validation.md))
  - `x-amazon-apigateway-request-validators` and `…-request-validator`
  - `x-amazon-apigateway-gateway-responses` (the Problem Details templates from ADR 0007)
- **The result** is passed to `SpecRestApi`.
- **Normal CDK still covers** the stage, deployment, WAF association and Lambda invoke permissions.

**Schema rules**, so that one schema works as an OpenAPI schema, an API Gateway model (JSON Schema draft-04) and a `fastjsonschema` draft-04 schema in the Lambda:
- **Separate request and response schemas** (`CreateOrderRequest`, `UpdateOrderStatusRequest` vs `Order`). No `readOnly`/`writeOnly`, which validators don't honour.
- **No `nullable`.** It is OpenAPI-specific, not draft-04.
- **`pattern` instead of `format`** for anything that must be enforced. Draft-04 validators generally don't enforce `format`.
- **`additionalProperties: false` on request and response objects.** A new response field therefore needs a spec change first: we own both sides of the contract.
- **Request schemas are self-contained, with no `$ref` at all.** Floci 2.3.0 rejects every body when a gateway request model contains any `$ref` (verified; see the verification document), so nested request objects such as line items are written inline. A unit test enforces it. Response schemas may still reference `#/components/schemas/…`.
- **Inside response schemas, `$ref` only to `#/components/schemas/…`.** Those are the references API Gateway turns into models and `fastjsonschema` resolves. Responses and headers may reference `#/components/responses` and `#/components/headers`, which validators never see.

**Guards.**
- **CI lints the document** with `openapi-spec-validator` and Spectral, including custom rules for the schema rules above.
- **A unit test compiles every request schema** with `fastjsonschema` as draft-04.

**Viewing it.** `make docs` starts a **Swagger UI container** on `localhost:8080`, showing the spec file. It is **read-only**: there is no "Try it out", because calling the API from the browser would need CORS. Requests are tried through an HTTP request collection instead. The README links to the spec file and to `make docs`.

**Proving the implementation matches.**
- **Response checks in every integration test:** each response is validated against the OpenAPI schema for its operation and status code (e.g. `openapi-core`). Undocumented fields, wrong types and undocumented status codes fail the test.
- **Property-based contract tests with Schemathesis** (`make contract-test`, also in CI) against the stack on Floci:
  - **Inputs:** requests generated from the spec, including unusual ones.
  - **Checks:** no `5xx`, documented status codes only, and responses matching their schemas.
  - **Setup:** an auth hook fetches a real token from Floci's Cognito, and the test environment has rate limits high enough not to dominate the results.

## Considered options

- **Routes as CDK code (`RestApi` + `addMethod`), with only the models generated from the document.**
  - *For:* idiomatic, typed CDK.
  - *Against:* the route table exists twice and needs a test to keep it equal.
  - *Verdict:* rejected in favour of one file defining routes, models, validation and errors.
- **Code-first** (Powertools Event Handler generating the spec). Rejected in ADR 0007: the document must be the source.
- **Putting the AWS extensions in the published document.** One file fewer to generate, but the contract would carry deployment details (ARNs, mapping templates) that clients don't need.
- **OpenAPI 3.1.** Its schemas are JSON Schema 2020-12, which API Gateway models don't accept. Revisit once API Gateway imports 3.1.
- **Ways to view the docs.**
  - *A `/api/v1/docs` route in the API:* an extra public route the assignment doesn't ask for.
  - *GitHub Pages:* needs a paid plan for the private working repo, and the URL would break when moving repos. A candidate for the final public repo.
  - *A static Redoc HTML file in the repo:* GitHub shows HTML as source, and it is one more generated artefact to keep in sync.
- **"Try it out" in Swagger UI.** Needs CORS on the API (and `OPTIONS` handling in the spec), a cross-cutting decision not taken for this. The request collection covers the need.
- **Only one of the two contract checks.** Response checks alone miss inputs we didn't think of. Schemathesis alone leaves the hand-written tests unchecked. If time gets tight, Schemathesis is the part to cut.
- **Open response objects** (allowing unknown fields). Friendlier to evolution for third-party consumers, but undocumented fields could appear silently. Strict objects make the contract the gate.

## Consequences

- **CDK wiring is more manual.** Integration ARNs and Lambda invoke permissions are wired explicitly instead of through `addMethod`.
- **To verify on Floci:**
  - that `SpecRestApi` (CloudFormation `Body`) imports the document with its integration, authorizer, request validators and gateway responses, and serves it end to end. Floci's source and docs list support for all of these extensions, but it hasn't been run.
- **To verify against AWS:** that API Gateway's OpenAPI import accepts 3.0.3 as used (and not 3.1).
- **The docs are view-only locally.** Interactive calls from the docs would need CORS.
- **The test environment needs high rate limits** for Schemathesis. It is a separate configuration from the one used to test `429` quickly.
