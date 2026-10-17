---
status: accepted
---

# Testing strategy: five suites, real tokens plus forged attacks, the same tests on Floci and AWS

Tests are organised in **five suites**. Each proves something the others can't:
- **unit tests** cover logic exhaustively and fast
- **adapter tests** cover the real AWS adapters against Floci
- **integration tests** drive the deployed stack end to end
- **contract tests** generate requests from the OpenAPI document
- **static checks** guard the architecture

Each of the assignment's five required categories (successful requests, invalid tokens, authorization failures, rate limiting, invalid input) is covered **twice**: exhaustively in unit tests and for real end to end. **The same test code runs against Floci and against a real AWS deployment**; the environment only changes the target. CI runs everything against Floci on every push.

## Decision

**Suites.**

| Suite | Runs against | Proves |
|---|---|---|
| **Unit** (`tests/unit`) | Plain Python; in-memory fakes for every port ([ADR 0009](0009-code-structure.md)) | Status changes and totals, the permission matrix, rate-limit decisions (tiers, IPv6 `/64`, fallback, circuit breaker), **each of the nine token checks on its own** (own test keys + a fake JWKS), the JWKS cache (refetch limit, stale keys, `503`), idempotency in progress, the request-pipeline order, Problem Details mapping |
| **Adapter** (`tests/adapter`) | The real outbound adapters, called directly, **against Floci** (not `moto`) | Conditional writes, both GSIs, cursor stability with equal timestamps, the atomic counter `ADD`, the SSM config source, idempotency persistence |
| **Integration** (`tests/integration`) | HTTP against the **deployed stack**: gateway → authorizer → API function → DynamoDB | The five assignment categories through the real path; every row of the [ADR 0007](0007-validation-and-errors.md) edge-case table; gateway responses; idempotency replay and mismatch; races; pagination walks. **Every response is validated against OpenAPI** ([ADR 0008](0008-openapi-contract.md)) |
| **Contract** (`tests/contract`) | Schemathesis against the deployed stack | Generated requests: no `5xx`, only documented status codes, responses match their schemas |
| **Static** | The repository | Ruff, mypy, import-linter, OpenAPI lint, request schemas compile as draft-04, `cdk-nag` at synth ([ADR 0012](0012-observability-and-security-baseline.md)) |

**Tokens.**
- **Unit tests isolate each of the nine checks.** A provider can't produce some combinations, e.g. a validly signed token with the wrong issuer.
- **End-to-end tests use the realistic shapes:**

| Case | Source |
|---|---|
| Valid, per role (alice, bob, manager, admin, nobody) | Real Cognito sign-in, cached per session and refreshed before expiry |
| Missing or malformed header | Made by the test |
| `alg: none`; `HS256` signed with the pool's **public** key | Forged |
| Real claims re-signed with our own key | Forged |
| Token from a **foreign user pool** | Real, from a test-fixture pool |
| ID token instead of an access token | Real |
| Token from an **untrusted app client** | Real, from a test-fixture client |
| Expired | Real, from a test-fixture client with a few-seconds lifetime (Floci only) |

- **Every forged or expired token is new to the authorizer,** so its 5-minute cache never hides a result ([ADR 0003](0003-access-token-validation.md)).
- **The fixture pool and clients exist only where the environment sets `testFixtures: true`.** The foreign pool and the untrusted client are deliberately not on the authorizer's allow-list. The **short-lived client is**: the expiry test first shows that a fresh token from that client is accepted, which needs a trusted client, and then that the same client's expired token is rejected. That control makes expiry the only possible cause of the second `401`.

**Determinism and isolation.**
- **Rate limits:**
  - **Every other suite runs with high limits.** The integration suite sends far more than 60 requests a minute per demo user, all from one address locally. A session fixture writes the `high` preset to the SSM parameter, waits out the cache, and writes ADR 0006's defaults back at the end, not the value it found, so a run killed halfway can't leave test limits in force for good. The deployed default stays ADR 0006's limits, so `make up` and a demo show the real limits; tests switch at runtime instead, which also demonstrates ADR 0006's runtime configuration.
  - **Setup:** each rate-limit module writes its tiny preset (`tiny`, or `tiny-ip` with the per-IP limit below every user limit for the per-IP test, since all local traffic shares one IP), waits for the 2-second cache, and restores the previous value afterwards, also when a test fails. Before each test it **empties the counter table**.
  - **Window boundaries:** if fewer than ~15 s remain in the current window, the fixture waits for the next one, so a test's requests (including a cache wait inside the test) fall into one window.
  - **Test-only helpers:** switching and waiting, and emptying the counters, refuse to run unless `testFixtures: true`, and use developer or CI credentials. `make limits` itself is the operators' tool and works everywhere.
  - **Isolation:** rate-limit tests run on their own, one after another within the session.
- **Orders:**
  - **Own data:** each test creates its own orders and asserts on their ids.
  - **Lists:** list tests walk all pages and check their own orders appear newest-first. "A USER sees only their own" is checked as "every returned `ownerId` is the caller's `sub`".
  - **No per-test cleanup:** local storage starts fresh on each `make up`, and `aws-dev` is destroyed after the run.
- **Eventual consistency:** list checks right after a write poll for up to 5 s ([ADR 0005](0005-dynamodb-storage.md)). Reads by id never poll.
- **Time:** domain tests set time through the `Clock` port. Token-validator tests use the real clock with margins of several seconds either side of the 30-second tolerance, because PyJWT reads the clock itself; `time-machine` stays the option if those margins ever prove flaky.
- **Races:** two simultaneous `PATCH`es must yield exactly one `200` and one `409`. The test is repeated several times.

**Targets.**
- **Environments:** `local` (Floci) and `aws-dev` (real AWS: seeding and test fixtures allowed, destroy removal policies, 2-second config cache). `aws-prod` remains the hardened example ([ADR 0010](0010-infrastructure-and-environments.md)).
- **Endpoints:** `make up ENV=<env>` writes `build/endpoints.json` (API URL, pool and client ids, region). Every suite reads only that file.
- **Running:** `make test-integration ENV=aws-dev` and `make contract-test ENV=aws-dev` run the identical tests against AWS.
- **Markers for target-specific tests:**
  - `floci_only`: short-lived real tokens. AWS's minimum token lifetime is 5 min, so on AWS expiry is covered by unit tests and forged tokens.
  - `aws_only`:
    - **WAF really blocks** (`aws-dev` sets a deliberately low WAF limit)
    - **`sourceIp` is the client's address behind the edge-optimized endpoint**
    - per-IP limiting from a real address

  The `aws_only` tests turn the "verify on the first AWS deploy" items from [ADR 0006](0006-rate-limiting.md) and ADR 0007 into executable checks.
- **Credentials:** a local AWS profile, or GitHub OIDC in CI.
- **Cost:** mostly pay-per-request pennies. WAF carries a small monthly fee per web ACL and rule, charged pro rata. The README states both.

**Tooling.**
- **Running and checking:** pytest (with markers: `adapter`, `integration`, `contract`, `floci_only`, `aws_only`), httpx, and **openapi-core** wrapped in one helper that every integration test calls.
- **Contract tests:** Schemathesis, with a Cognito auth hook.
  - **Version and wiring:** Schemathesis 4 through its pytest integration (`tests/contract`), so the suite shares the endpoints, tokens and the `high` limits switch with the integration tests, writes JUnit like them, and runs as `make contract-test`. Settings live in `tests/contract/schemathesis.toml`.
  - **Callers:** every operation is generated twice, as `admin` (every permission, so deletes and any status change reach the function's success) and as `alice` (a `USER`: the owner-only list index, own orders only, the `403`s, someone else's order as a `404`). Schemathesis itself removes or spoils the token where it tests that a token is required.
  - **Orders to find:** random ids would only ever be `404`s. Before each caller's run, fixed-id orders are written straight to the table (one of alice's in each status, one of bob's), and half the generated `{id}`s are drawn from them. Deletes draw from two orders of their own, so they can't remove the ones the other operations need.
  - **Generation:** positive and negative requests; the examples, coverage and fuzzing phases with 100 fuzzed examples per operation and caller; deterministic (no random seed, no example database), so two runs send the same requests. About 1,500 requests, 40 s against Floci locally.
  - **Checks:** Schemathesis's defaults: no `5xx`, documented status codes, content types, headers (required ones present, values matching their schemas) and bodies matching their schemas; invalid requests rejected; a token required. Two are switched off, each for a decision taken elsewhere: `unsupported_method` expects `405` with `Allow`, where ADR 0007 answers `404` (checked by the integration tests); `positive_data_acceptance` expects every schema-valid request to succeed, where duplicate SKUs, a cursor not from a previous page, a reused `Idempotency-Key` and a stale `If-Match` are refused by design (each covered by integration tests).
  - **The run proves it reached the function:** the suite prints how often each caller got each status from each operation, and fails if some operation never answered its success status.
  - **Secrets:** Schemathesis's output sanitization stays on, so failure reports and reproduction commands show `Authorization: [Filtered]`. CI uploads a report only after checking it contains no token.
  - **Demonstrated** (2026-10-17, Schemathesis 4.31.1, Floci 2.3.0), each on a scratch change that was reverted:
    - *Removing a documented status code* (`404` from `GET /api/v1/orders/{id}`): both callers' runs of that operation failed with `Undocumented HTTP status code — Received: 404, Documented: 200, 400, 401, 403, 413, 429, 500, 502, 503, 504`.
    - *Adding an undocumented response field* (`internalNote` in the order JSON, deployed): every operation returning an order failed with `Response violates schema — Additional properties are not allowed ('internalNote' was unexpected)`, validated against `#/components/schemas/Order`.
- **Tokens and time:** PyJWT + cryptography for forging; the real clock with safe margins for time checks.
- **Fixtures:** boto3.
- **Property-based domain tests with Hypothesis:**
  - random sequences of requested status changes never produce an invalid status, a skipped step or a change after a final status
  - totals always equal the sum over the line items
- **Coverage:** pytest-cov (branch), measured from the unit and adapter suites. Integration and contract tests execute code inside Floci's Lambda containers and are evidence by behaviour, not by percentage.
  - **≥ 95 % for `domain`, `application` and `auth`**
  - **≥ 85 % overall**
  - enforced in CI
  - **What is measured:** the shipped package `orders_api` (`src/`). The CDK app and its scripts (`infra/`) are exercised by every `make up` and by their own unit tests, but are not part of the figure.
  - **How:** `make test` starts the coverage data afresh, `make test-adapter` adds to it, and `make coverage` enforces the thresholds (`COVERAGE_MIN`, `COVERAGE_CORE_MIN` in the Makefile): once over everything, then once per core package. It also writes the XML and HTML reports.
  - **In CI** the `unit` job enforces them on the unit suite's coverage alone, before Floci is started. Adapter tests only add coverage, so this is the stricter check; locally `make test test-adapter coverage` reports both suites together.

**CI/CD (GitHub Actions).**
- **`ci.yml`:** on every push and pull request; a new push cancels the previous run.
  1. **`static`.**
  2. **`unit`:** with coverage thresholds; JUnit and coverage reports uploaded.
  3. **`floci`:** after the first two pass.
     - **Setup:** `make up` (Compose, pinned Floci, `cdk deploy --method=direct`, seed).
     - **Order:** adapter → integration → contract.
     - **Speed:** Floci's Lambda base image is pre-pulled to avoid first-invoke delays, and uv and npm downloads are cached.
     - **On failure:** Floci and Lambda logs are uploaded.
     - **Always:** `make down`.
  - The runner is x86, handled by the host-architecture rule (ADR 0010).
  - An **arm64 runner** is optional, for the public repo, where it is free.
- **`aws-e2e.yml`:** **manual trigger only.**
  - **Access:** GitHub OIDC → an IAM role, no stored keys.
  - **Steps:** deploy `aws-dev`, seed, integration + contract tests (including `aws_only`), and **`cdk destroy` in an always-run step**.
- **Branch protection** on `main` requires `static`, `unit` and `floci`.
- **No continuous deployment to production.** Deploying to AWS is optional and manual; CD belongs to production evolution.

## Considered options

- **`moto` for adapter tests.** It needs no Docker and is very fast, but it would be a second emulator with its own quirks, able to pass where Floci fails or the other way round. One emulator means one source of truth, with no `boto3` mocks anywhere but one: failures the emulator can't produce on cue (a throttled key, a hanging or unreachable table) are injected with botocore's `Stubber` and local sockets that never answer, in the unit tier (`tests/unit/test_dynamodb_rate_limit_failures.py`). They prove the counter adapter's error translation and time budget ([ADR 0006](0006-rate-limiting.md)), not any DynamoDB behaviour.
- **Integration tests that call the handlers in-process.** Faster, but they skip the gateway validators, Gateway Responses and the authorizer cache, which several ADRs depend on.
- **Isolating every token check end to end.** Impossible with a real provider: only Cognito can sign, and its key id is tied to the pool. Unit tests isolate the checks, and end-to-end tests cover realistic attacks.
- **Separate deployments per test suite** (tiny or high limits). Replaced by switching the runtime SSM parameter, which also demonstrates ADR 0006.
- **Waiting out rate-limit windows, or a fresh user per test,** instead of emptying the counter table. Slower, or it would need many seeded users.
- **Coverage including the integration tests.** It would need coverage collection inside the Lambda containers on Floci: complex, and it says little beyond the behavioural evidence.
- **Running the AWS workflow on every push.** Slow (an edge-optimized deploy takes extra minutes), costly, and it would need AWS credentials on every branch. A manual trigger fits a verification step.

## Consequences

- **Rate-limit tests and suites that switch the SSM limits run one after another,** not in parallel, against the single deployment.
- **Two pieces of test-only infrastructure** exist only when `testFixtures: true`, never in `aws-prod`:
  - the fixture pool and clients
  - the counter-emptying helper
- **The "verify on AWS" items are executable** (`aws_only`), but only run when someone triggers the AWS workflow or runs `ENV=aws-dev` locally.
- **The idempotency "in progress" case is covered by unit tests only,** because it depends on timing end to end.
- **Coverage figures describe in-process code only.** The deployed path is proven by the integration and contract suites.
