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
make up                 # start Floci, deploy the stack, write build/endpoints.json
make test               # unit tests
make test-integration   # HTTP tests against the deployed stack
make down               # stop and remove everything
```

`make up` prints the local API URL, for example
`http://localhost:4566/restapis/<id>/live/_user_request_`. Then:

```sh
curl "$(jq -r .apiUrl build/endpoints.json)/api/v1/health"
```

Other targets: `make lint` (Ruff, mypy, import-linter, cdk-nag), `make docs` (read-only Swagger
UI on http://localhost:8080). `make help` lists them all.
