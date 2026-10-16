# Verification of design assumptions

Some design decisions rest on emulator or AWS behaviour that was read in source code or
documentation before it could be run. This table tracks each one until a named test proves it.
If a check fails, the ADR it comes from is revisited (amended or superseded).

**Status:** `verified` (the named check passes) · `pending` (not yet run) · `failed`.

| Assumption | Target | How it's checked | Status | Version / date |
|---|---|---|---|---|
| `SpecRestApi` imports the OpenAPI document with its Lambda proxy integration ([ADR 0008](adr/0008-openapi-contract.md)) | Floci | `tests/integration/test_health.py` | verified | Floci 2.3.0, 2026-10-16 |
| Gateway Responses render as Problem Details with the security headers; `MISSING_AUTHENTICATION_TOKEN` becomes `404` ([ADR 0007](adr/0007-validation-and-errors.md)) | Floci | `tests/integration/test_gateway_responses.py` | verified | Floci 2.3.0, 2026-10-16 |
| A static `Content-Security-Policy` value containing single quotes renders unchanged in Gateway Responses ([ADR 0012](adr/0012-observability-and-security-baseline.md)) | Floci | `tests/integration/test_gateway_responses.py` (header check) | verified | Floci 2.3.0, 2026-10-16 |
| The same, on AWS | AWS | `aws_only` integration run | pending | — |
| `DEFAULT_4XX` / `DEFAULT_5XX` render as `about:blank` Problem Details via `$context.error.messageString` ([ADR 0007](adr/0007-validation-and-errors.md)) | Floci + AWS | Not yet triggerable locally; to be covered once a route can produce a default gateway error | pending | — |
| `INTEGRATION_FAILURE` → `502` and `INTEGRATION_TIMEOUT` → `504` render as Problem Details ([ADR 0007](adr/0007-validation-and-errors.md)) | Floci | To be covered by a failing-integration test | pending | — |
| Host-architecture bundling works on arm64 hosts ([ADR 0010](adr/0010-infrastructure-and-environments.md)) | Floci | `make up` + integration tests on an arm64 machine | verified | Floci 2.3.0, aarch64, 2026-10-16 |
| Host-architecture bundling works on x86_64 hosts ([ADR 0010](adr/0010-infrastructure-and-environments.md)) | Floci | The `floci` CI job (x86_64 runner) | pending | — |
| Redeploys succeed with `cdk deploy --method=direct` ([ADR 0010](adr/0010-infrastructure-and-environments.md)) | Floci | A second `make deploy` | verified | Floci 2.3.0, CDK CLI 2.1145.0, 2026-10-16 |
| The Floci setting that puts Lambda containers on the Compose network (`FLOCI_SERVICES_LAMBDA_DOCKER_NETWORK`) ([ADR 0010](adr/0010-infrastructure-and-environments.md)) | Floci | Authorizer reaching the issuer at `http://floci:4566` | pending | — |
| `AWS::SecretsManager::Secret` with `GenerateSecretString` ([ADR 0010](adr/0010-infrastructure-and-environments.md)) | Floci | Seeding users from the generated secret | pending | — |
| Whether the authorizer runs before request validation; non-JSON `Content-Type` on a validated method ([ADR 0007](adr/0007-validation-and-errors.md)) | Floci + AWS | Invalid-input integration tests | pending | — |
| WAF's `IP` key and `sourceIp` see the client behind the edge-optimized endpoint ([ADR 0006](adr/0006-rate-limiting.md)) | AWS | `aws_only` tests | pending | — |
| WAF's IPv6 aggregation ([ADR 0006](adr/0006-rate-limiting.md)) | AWS | `aws_only` tests | pending | — |
| API Gateway imports the OpenAPI 3.0.3 document as used ([ADR 0008](adr/0008-openapi-contract.md)) | AWS | `aws-dev` deploy | pending | — |
