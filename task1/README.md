# Task 1: Orders API

Orders REST API with Amazon Cognito authentication, role-based authorization and rate limiting.
It runs locally on the [Floci](https://github.com/floci-io/floci) AWS emulator, and the same AWS
CDK stack deploys to AWS. The design is in [`docs/adr/`](docs/adr/); open assumptions are tracked
in [`docs/verification.md`](docs/verification.md).

> Work in progress: the full README (deliverables at a glance, demo, test guide, AWS
> deployment) arrives with the reviewer documentation.

## Prerequisites

| Tool | Version | Why |
|---|---|---|
| Docker with Compose | Docker 24+, Compose 2.20+ | Runs Floci and its Lambda containers |
| `make` | 3.81+ | The setup and execution targets |
| [`uv`](https://docs.astral.sh/uv/) | 0.8+ | Python tooling; installs Python 3.13 (pinned in `.python-version`) |
| Node.js and npm | Node 20+, npm 10+ | Runs the AWS CDK CLI, pinned to 2.1145.0 in `infra/package.json` |

Check them with:

```sh
make doctor
```

## Quick start

```sh
make up                 # start Floci, deploy the stack, seed demo users, write build/endpoints.json
make test               # unit tests
make test-adapter       # DynamoDB adapter against Floci
make test-integration   # HTTP tests against the deployed stack
make contract-test      # Schemathesis: generated requests against the deployed stack
make coverage           # coverage thresholds of the unit (and adapter) runs, XML and HTML reports
make down               # stop and remove everything
```

`make up` prints the local API URL, for example
`http://localhost:4566/restapis/<id>/live/_user_request_`. Then:

```sh
API=$(jq -r .apiUrl build/endpoints.json)
curl "$API/api/v1/health"
curl -H "Authorization: Bearer $(make -s token ROLE=user)" "$API/api/v1/orders/<id>"
```

`make token` prints an access token for a demo user: `ROLE=user|manager|admin|none`, or
`USER_NAME=alice|bob|manager|admin|nobody`. The demo users' password is generated into Secrets
Manager (`/orders-api/local/seed-users`); it never appears in the repository.

Rate limits are runtime configuration (ADR 0006): every order request counts per client IP and
per user, and over a limit the API answers `429`. `make limits PRESET=…` switches them without a
deploy, in force within the cache time (2 s locally): `defaults` (300 per IP, 60 per `USER`, 300
for staff, per minute), `high` (what the integration tests run with), `tiny` (a `USER` is refused
after 3 requests) or `tiny-ip` (the IP after 3). Responses carry `RateLimit-Limit`,
`RateLimit-Remaining` and `RateLimit-Reset`.

Other targets: `make lint` (Ruff, mypy, import-linter, the OpenAPI lint of `make lint-openapi`,
cdk-nag), `make docs` (read-only Swagger UI on http://localhost:8080). `make help` lists them all.
The test targets pass `PYTEST_ARGS` on to pytest, e.g. `make contract-test PYTEST_ARGS=-x`.
