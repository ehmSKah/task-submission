# Setup and execution for task2 (ADR 0009). `make help` lists the targets.

# The host this machine reaches published ports by: the remote Docker daemon's host when
# DOCKER_HOST points at one (tcp://dind:2375 -> dind), otherwise 127.0.0.1: not localhost, which
# may resolve to IPv6 first while Docker publishes ports on IPv4.
STACK_HOST ?= $(or $(shell printf '%s' "$$DOCKER_HOST" | sed -n 's|^tcp://\([^:/]*\).*|\1|p'),127.0.0.1)
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
# The assignment's four test categories: the files `make demo`'s sections name, in their order.
DEMO_TESTS := tests/integration/test_successful_processing.py tests/integration/test_invalid_messages.py tests/integration/test_duplicate_events.py tests/integration/test_downstream_failures.py
ACTIONLINT_IMAGE ?= rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667

.DEFAULT_GOAL := help
.PHONY: help install up up-tracing down clean ps logs psql publish replay demo demo-tests lint \
	lint-workflows test test-unit test-integration test-reliability

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

# Docker keeps each health check's output; the last one says why a consumer is unhealthy.
ps: ## Show the stack's services, their health, and each consumer's last health check
	$(COMPOSE) ps --all
	@for id in $$($(COMPOSE) ps --quiet consumer); do \
		name=$$(docker inspect --format '{{.Name}}' $$id); \
		check=$$(docker inspect --format \
			'{{with .State.Health}}{{range .Log}}{{.Output}}{{end}}{{end}}' $$id | grep . | tail -n 1); \
		echo "$${name#/}: $${check:-no health check yet}"; \
	done

logs: ## Follow the logs of every service
	$(COMPOSE) logs --follow

psql: ## Open a SQL shell on the orders database (as the migration owner)
	$(COMPOSE) exec postgres psql -U orders_owner -d orders

# On the host, like the tests: each sample is a new order with its own traceparent.
publish: ## Publish sample events with a traceparent; ARGS="--help" lists the kinds (default: one valid)
	uv run python -m demo.publish $(ARGS)

# New orders every run, so it repeats on the same stack without `make clean`.
demo: ## The assignment's four test categories, live against the running stack (`make up` first)
	uv run python -m demo

# In a one-off consumer container, so it reaches Kafka as the consumers do, with a remote daemon too.
replay: ## Replay dead letters (ADR 0006), a dry run unless ARGS contains --execute; ARGS="--help" lists the filters
	$(COMPOSE) run --rm --no-deps consumer python -m order_consumer.replay $(ARGS)

demo-tests: ## The four test files proving the demo's sections (`make up` first)
	$(PYTEST) $(DEMO_TESTS) $(PYTEST_ARGS)

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

test-unit: ## Unit tests of the consumer, the stub, the demo and the docs, with the core coverage gate (no stack needed)
	@mkdir -p build
	$(PYTEST) consumer/tests fulfilment-stub/tests demo/tests tests/docs --cov --cov-report=term $(PYTEST_ARGS)

test-integration: ## Integration tests against the running stack (`make up` first)
	$(PYTEST) tests/integration $(PYTEST_ARGS)

test-reliability: ## Kill and restart the stack's consumer containers (`make up` first)
	$(PYTEST) tests/reliability $(PYTEST_ARGS)
