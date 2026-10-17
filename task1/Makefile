# Setup and execution for the Orders API (ADR 0010). `make help` lists the targets.

ENV ?= local
FLOCI_URL ?= http://localhost:4566

# Each run reaches one target only (ADR 0010). `local` is Floci: its endpoint and dummy
# credentials, whatever the shell holds, so no local target can reach a real account. Any other
# ENV is real AWS: the caller's own credentials (a profile, SSO or GitHub OIDC) in the
# environment's region, and never an endpoint override. `guard` refuses whatever make can't unset
# (e.g. AWS_ENDPOINT_URL_<SERVICE>) before a target touches the environment.
ifeq ($(ENV),local)
export AWS_ACCESS_KEY_ID := test
export AWS_SECRET_ACCESS_KEY := test
export AWS_REGION := us-east-1
export AWS_DEFAULT_REGION := us-east-1
export AWS_ENDPOINT_URL := $(FLOCI_URL)
unexport AWS_SESSION_TOKEN AWS_PROFILE
else
unexport AWS_ENDPOINT_URL
export AWS_REGION := $(shell sed -n 's/^region: *//p' infra/config/$(ENV).yaml 2>/dev/null)
export AWS_DEFAULT_REGION := $(AWS_REGION)
endif

CDK := cd infra && npx --no-install cdk
RUN_INFRA_MODULE := cd infra && uv run python -m

.DEFAULT_GOAL := help
.PHONY: help doctor guard install up floci-up bootstrap bundle deploy endpoints seed token down \
	limits demo http-env test test-adapter test-integration contract-test coverage lint \
	lint-openapi lint-workflows docs

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

doctor: ## Check the host tools; for an AWS ENV also the credentials and the account's setup
ifeq ($(ENV),local)
	@scripts/doctor.sh floci
else
	@scripts/doctor.sh aws
	@$(RUN_INFRA_MODULE) orders_api_infra.preflight --env $(ENV) --account
endif

guard: ## Refuse to run when the shell would reach anything but ENV's target
	@$(RUN_INFRA_MODULE) orders_api_infra.preflight --env $(ENV)

install: ## Install Python and CDK CLI dependencies
	uv sync --frozen
	npm ci --prefix infra --no-audit --no-fund

ifeq ($(ENV),local)
UP_STEPS := install floci-up bootstrap deploy endpoints seed
DOWN_STEPS :=
else
UP_STEPS := install doctor bootstrap deploy endpoints seed
DOWN_STEPS := guard bundle
endif

up: $(UP_STEPS) ## Deploy and seed users: on Floci, started first (ENV=local), or on AWS (ENV=aws-dev)

floci-up: ## Start the Floci emulator
	docker compose up -d floci
	@scripts/wait-for-floci.sh $(FLOCI_URL)

# Bootstraps the account and region the environment config names (idempotent).
bootstrap: guard bundle ## Bootstrap CDK in the target account
	$(CDK) bootstrap -c env=$(ENV) --quiet

bundle: ## Build the Lambda code bundle for the target architecture
	$(RUN_INFRA_MODULE) orders_api_infra.bundle --env $(ENV)

# --method=direct: Floci breaks CDK's default change-set redeploys (ADR 0010); AWS takes it too.
deploy: guard bundle ## Deploy the stack
	$(CDK) deploy -c env=$(ENV) --method=direct --require-approval never \
		--outputs-file ../build/cdk-outputs.json

endpoints: guard ## Write build/endpoints.json from the stack outputs
	$(RUN_INFRA_MODULE) orders_api_infra.endpoints --env $(ENV) --floci-url $(FLOCI_URL)

seed: guard ## Create the demo users (idempotent; refused where seedUsers is false)
	$(RUN_INFRA_MODULE) orders_api_infra.demo_users seed --env $(ENV)

ROLE ?= user
token: guard ## Print an access token: ROLE=user|manager|admin|none, or USER_NAME=alice|bob|…
	@$(RUN_INFRA_MODULE) orders_api_infra.demo_users token --env $(ENV) --role $(ROLE) \
		$(if $(USER_NAME),--user $(USER_NAME))

PRESET ?= defaults
limits: guard ## Switch the runtime rate limits, no deploy: PRESET=defaults|high|tiny|tiny-ip
	$(RUN_INFRA_MODULE) orders_api_infra.rate_limits --env $(ENV) --preset $(PRESET)

# Needs the environment's testFixtures: the rate-limit step empties the counters and waits out
# the cache, as the test suites do (ADR 0011).
demo: guard ## Run the one-minute scenario against the deployed stack; fails on any unexpected status
	$(RUN_INFRA_MODULE) orders_api_infra.demo --env $(ENV)

# Floci leaves sibling containers (e.g. its ECR registry) on the network; remove those too. On
# AWS, destroying needs the app to synthesise, hence the bundle; aws-prod's termination protection
# refuses it.
down: $(DOWN_STEPS) ## Stop Floci and remove everything (ENV=local), or destroy the AWS stack
ifeq ($(ENV),local)
	docker compose --profile docs down -v --remove-orphans || true
	docker ps -aq --filter label=floci=true | xargs -r docker rm -f >/dev/null
	docker compose down -v --remove-orphans
else
	$(CDK) destroy -c env=$(ENV) --force
endif

PYTEST_ARGS ?=

# Both suites collect branch coverage into build/.coverage; the unit run starts it afresh and the
# adapter run adds to it. `make coverage` enforces the thresholds on whatever was collected.
test: ## Unit tests, collecting coverage
	uv run pytest tests/unit --cov --cov-report= $(PYTEST_ARGS)

test-adapter: guard ## Adapter tests against the emulator, adding to the coverage
	uv run pytest tests/adapter -m adapter --cov --cov-append --cov-report= $(PYTEST_ARGS)

test-integration: guard ## Integration tests against the deployed stack
	uv run pytest tests/integration -m integration $(PYTEST_ARGS)

contract-test: guard ## Schemathesis contract tests against the deployed stack
	uv run pytest tests/contract -m contract $(PYTEST_ARGS)

# ADR 0011's gates, from the unit and adapter suites: the packages that hold the rules need more
# than the adapters around them.
COVERAGE_MIN ?= 85
COVERAGE_CORE_MIN ?= 95
COVERAGE_CORE_PACKAGES := domain application auth

coverage: ## Enforce the coverage thresholds (after make test); writes XML and HTML reports
	uv run coverage xml -q
	uv run coverage html -q
	uv run coverage report --fail-under=$(COVERAGE_MIN)
	@for package in $(COVERAGE_CORE_PACKAGES); do \
		echo "orders_api.$$package (at least $(COVERAGE_CORE_MIN) %):"; \
		uv run coverage report --include="src/orders_api/$$package/*" \
			--fail-under=$(COVERAGE_CORE_MIN) || exit 1; \
	done

# Every environment is synthesised, so cdk-nag checks the AWS ones too. Synth needs no AWS
# credentials: AWS environments leave the account to deploy time (ADR 0010).
SYNTH_ENVS := local aws-dev aws-prod

lint: lint-openapi lint-workflows bundle ## Ruff, mypy, import-linter, OpenAPI and workflow lint, cdk-nag
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run lint-imports
	@for env in $(SYNTH_ENVS); do \
		echo "cdk synth -c env=$$env"; \
		($(CDK) synth -c env=$$env --quiet) || exit 1; \
	done

# The schema rules of ADR 0008 are custom Spectral rules in openapi/.spectral.yaml.
lint-openapi: ## Validate the OpenAPI document and lint it with Spectral
	uv run openapi-spec-validator openapi/openapi.yaml
	cd infra && npx --no-install spectral lint ../openapi/openapi.yaml \
		--ruleset ../openapi/.spectral.yaml --fail-severity warn

# actionlint, pinned by digest. The workflows are piped in, so no bind mount (or SELinux relabelling)
# is needed; the image brings shellcheck for the run steps.
ACTIONLINT_IMAGE := rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667

lint-workflows: ## Check the GitHub Actions workflows with actionlint, without running them
	@for workflow in ../.github/workflows/*.yml; do \
		echo "actionlint $${workflow#../}"; \
		docker run --rm -i $(ACTIONLINT_IMAGE) -stdin-filename "$${workflow#../}" - \
			< "$$workflow" || exit 1; \
	done

docs: ## Serve the OpenAPI document in a read-only Swagger UI on http://localhost:8080
	docker compose --profile docs up -d swagger-ui
	@echo "Swagger UI: http://localhost:8080"
