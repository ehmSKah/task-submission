# Setup and execution for the Orders API (ADR 0010). `make help` lists the targets.

ENV ?= local
FLOCI_URL ?= http://localhost:4566

ifeq ($(ENV),local)
export AWS_ACCESS_KEY_ID ?= test
export AWS_SECRET_ACCESS_KEY ?= test
export AWS_REGION ?= us-east-1
export AWS_DEFAULT_REGION ?= us-east-1
export AWS_ENDPOINT_URL ?= $(FLOCI_URL)
endif

CDK := cd infra && npx --no-install cdk
RUN_INFRA_MODULE := cd infra && uv run python -m

.DEFAULT_GOAL := help
.PHONY: help doctor install up floci-up bootstrap bundle deploy endpoints seed token down \
	limits test test-adapter test-integration contract-test coverage lint lint-openapi docs

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

doctor: ## Check the required host tools
	@scripts/doctor.sh

install: ## Install Python and CDK CLI dependencies
	uv sync --frozen
	npm ci --prefix infra --no-audit --no-fund

up: install floci-up bootstrap deploy endpoints seed ## Start Floci, deploy and seed users (ENV=local)

floci-up: ## Start the Floci emulator
	docker compose up -d floci
	@scripts/wait-for-floci.sh $(FLOCI_URL)

# Bootstraps the account and region the environment config names (idempotent).
bootstrap: bundle ## Bootstrap CDK in the target account
	$(CDK) bootstrap -c env=$(ENV) --quiet

bundle: ## Build the Lambda code bundle for the target architecture
	$(RUN_INFRA_MODULE) orders_api_infra.bundle --env $(ENV)

# --method=direct: Floci breaks CDK's default change-set redeploys (ADR 0010).
deploy: bundle ## Deploy the stack
	$(CDK) deploy -c env=$(ENV) --method=direct --require-approval never \
		--outputs-file ../build/cdk-outputs.json

endpoints: ## Write build/endpoints.json from the stack outputs
	$(RUN_INFRA_MODULE) orders_api_infra.endpoints --env $(ENV) --floci-url $(FLOCI_URL)

seed: ## Create the demo users (idempotent; refused where seedUsers is false)
	$(RUN_INFRA_MODULE) orders_api_infra.demo_users seed --env $(ENV)

ROLE ?= user
token: ## Print an access token: ROLE=user|manager|admin|none, or USER_NAME=alice|bob|…
	@$(RUN_INFRA_MODULE) orders_api_infra.demo_users token --env $(ENV) --role $(ROLE) \
		$(if $(USER_NAME),--user $(USER_NAME))

PRESET ?= defaults
limits: ## Switch the runtime rate limits, no deploy: PRESET=defaults|high|tiny|tiny-ip
	$(RUN_INFRA_MODULE) orders_api_infra.rate_limits --env $(ENV) --preset $(PRESET)

# Floci leaves sibling containers (e.g. its ECR registry) on the network; remove those too.
down: ## Stop Floci and remove its containers, network and volumes
	docker compose --profile docs down -v --remove-orphans || true
	docker ps -aq --filter label=floci=true | xargs -r docker rm -f >/dev/null
	docker compose down -v --remove-orphans

PYTEST_ARGS ?=

# Both suites collect branch coverage into build/.coverage; the unit run starts it afresh and the
# adapter run adds to it. `make coverage` enforces the thresholds on whatever was collected.
test: ## Unit tests, collecting coverage
	uv run pytest tests/unit --cov --cov-report= $(PYTEST_ARGS)

test-adapter: ## Adapter tests against the emulator, adding to the coverage
	uv run pytest tests/adapter -m adapter --cov --cov-append --cov-report= $(PYTEST_ARGS)

test-integration: ## Integration tests against the deployed stack
	uv run pytest tests/integration -m integration $(PYTEST_ARGS)

contract-test: ## Schemathesis contract tests against the deployed stack
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

lint: lint-openapi bundle ## Ruff, mypy, import-linter, OpenAPI lint and cdk-nag (via cdk synth)
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run lint-imports
	$(CDK) synth -c env=$(ENV) --quiet

# The schema rules of ADR 0008 are custom Spectral rules in openapi/.spectral.yaml.
lint-openapi: ## Validate the OpenAPI document and lint it with Spectral
	uv run openapi-spec-validator openapi/openapi.yaml
	cd infra && npx --no-install spectral lint ../openapi/openapi.yaml \
		--ruleset ../openapi/.spectral.yaml --fail-severity warn

docs: ## Serve the OpenAPI document in a read-only Swagger UI on http://localhost:8080
	docker compose --profile docs up -d swagger-ui
	@echo "Swagger UI: http://localhost:8080"
