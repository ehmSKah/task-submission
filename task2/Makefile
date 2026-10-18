# Setup and execution for task2 (ADR 0009). `make help` lists the targets.

# The host this machine reaches published ports by: the remote Docker daemon's host when
# DOCKER_HOST points at one (tcp://dind:2375 -> dind), otherwise localhost.
STACK_HOST ?= $(or $(shell printf '%s' "$$DOCKER_HOST" | sed -n 's|^tcp://\([^:/]*\).*|\1|p'),localhost)
export STACK_HOST

# Host ports and the Compose project, overridable so several stacks can share one Docker daemon.
KAFKA_PORT ?= 9094
POSTGRES_PORT ?= 5432
STUB_PORT ?= 8081
UI_PORT ?= 8080
JAEGER_UI_PORT ?= 16686
export KAFKA_PORT POSTGRES_PORT STUB_PORT UI_PORT JAEGER_UI_PORT

COMPOSE := docker compose
PYTEST := uv run pytest
PYTEST_ARGS ?=
ACTIONLINT_IMAGE ?= rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667

.DEFAULT_GOAL := help
.PHONY: help install up up-tracing down clean ps logs psql lint lint-workflows test test-unit test-integration \
	test-reliability

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

install: ## Install the Python dependencies
	uv sync --frozen

define URLS
	@echo "kafka-ui:         http://$(STACK_HOST):$(UI_PORT)"
	@echo "fulfilment stub:  http://$(STACK_HOST):$(STUB_PORT)/_control/fulfilment-orders"
	@echo "Kafka (host):     $(STACK_HOST):$(KAFKA_PORT)"
	@echo "PostgreSQL:       $(STACK_HOST):$(POSTGRES_PORT) (make psql)"
endef

up: ## Build and start the stack, and wait until it is healthy
	$(COMPOSE) up --build --detach --wait
	$(URLS)

up-tracing: ## The same, with Jaeger, and both services exporting their traces to it
	OTLP_ENDPOINT=http://jaeger:4318 $(COMPOSE) --profile tracing up --build --detach --wait
	$(URLS)
	@echo "Jaeger:           http://$(STACK_HOST):$(JAEGER_UI_PORT)"

# With the tracing profile, so Jaeger stops too when it was started.
down: ## Stop the stack, keeping its data
	$(COMPOSE) --profile tracing down

clean: ## Stop the stack and delete its data
	$(COMPOSE) --profile tracing down --volumes --remove-orphans

ps: ## Show the stack's services and their health
	$(COMPOSE) ps --all

logs: ## Follow the logs of every service
	$(COMPOSE) logs --follow

psql: ## Open a SQL shell on the orders database (as the migration owner)
	$(COMPOSE) exec postgres psql -U orders_owner -d orders

lint: lint-workflows ## Ruff, mypy (strict), the import rules and the CI workflow
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run lint-imports

# Piped over stdin, so it also works with a remote Docker daemon.
lint-workflows: ## Check the task2 CI workflow with actionlint
	@echo "actionlint .github/workflows/task2-ci.yml"
	@docker run --rm -i $(ACTIONLINT_IMAGE) -stdin-filename .github/workflows/task2-ci.yml - \
		< ../.github/workflows/task2-ci.yml

test: test-unit test-integration test-reliability ## Every suite

test-unit: ## Unit tests of the consumer and the stub, with the core coverage gate (no stack needed)
	@mkdir -p build
	$(PYTEST) consumer/tests fulfilment-stub/tests --cov --cov-report=term $(PYTEST_ARGS)

test-integration: ## Integration tests against the running stack (`make up` first)
	$(PYTEST) tests/integration $(PYTEST_ARGS)

test-reliability: ## Kill and restart the stack's consumer containers (`make up` first)
	$(PYTEST) tests/reliability $(PYTEST_ARGS)
