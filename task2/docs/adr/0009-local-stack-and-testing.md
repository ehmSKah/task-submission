---
status: accepted
---

# Local stack and testing: one Compose stack for reviewers, tests and CI

The **Compose stack is the deliverable**, so it is also what the tests run against: the same broker image, the same services, the same configuration apart from shorter timeouts. pytest runs on the host. Most integration tests run the consumer **in the test process** against the stack's Kafka, PostgreSQL and stub; the lifecycle scenarios drive the **real consumer containers**. The assignment's four test categories each have **one file named after them**. No test sleeps: every wait is for a state, and a failed wait reports what it saw. CI runs everything, including the demo, on every change to task2.

## Decision

**Harness**
- **`docker compose up --wait`** with `compose.test.yaml` layered on top, which shortens backoff caps, attempt deadlines, the probe interval and, for the zombie test, the poll interval.
- **In-process consumer tests.** pytest runs the consumer inside the test process against the stack's Kafka, PostgreSQL and stub. Each test module uses its own topics (`orders.test.<id>`, `orders.test.<id>.dlt`) and consumer group, so the stack's consumers never take its messages. The crash-point wrapper of ADR 0007 runs this way.
- **Container tests.** These kill, stop and scale the real consumer containers on the real `orders` topic.
- **Isolation:** every test uses fresh order ids, the stub's fault rules match per order (ADR 0003), and assertions look only at the test's own orders. No test depends on an empty database.

**Suites**

| Suite | Where | Runs against | Proves |
|---|---|---|---|
| Unit | `consumer/tests/unit` | plain Python with fakes behind every port and a fake clock | each validation layer and business rule; both mappings, with property tests (the total always equals the sum of line totals; money formats correctly for every currency exponent); duplicate versus conflict; retry policy and backoff; the flood guard's states; the outcome-to-commit table; the start-up budget check; log redaction |
| Stub unit | `fulfilment-stub/tests` | the stub in the test process | replay, `422`, `409` in progress, fault rules, the journal |
| Integration | `tests/integration` | the in-process consumer against the stack | the four assignment categories; crash points; dead-letter contents; replay; trace continuity (OpenTelemetry's in-memory span exporter); no token in logs or spans; one terminal log line per message |
| Reliability | `tests/reliability` | the consumer containers | hard kill mid-handover, graceful stop mid-stream, rebalance under load, zombie consumer, pause during a rebalance (ADR 0007) |
| Static | the repository | | ruff, mypy (strict), import-linter; the JSON Schema and OpenAPI files are valid and their examples pass |

**The assignment's four categories**, one integration file each:

| File | Cases |
|---|---|
| `test_successful_processing.py` | order stored and handed over; fields and money mapped; one terminal log line; one trace across the handover |
| `test_invalid_messages.py` | one case per validation layer: unreadable, not a CloudEvent, unsupported type or version, schema violation, business rule. Each ends as a dead letter naming the layer and errors, with nothing stored and nothing handed over |
| `test_duplicate_events.py` | redelivery, re-emission, a conflicting event (dead letter), two consumers racing on one order; in every case exactly one fulfilment order |
| `test_downstream_failures.py` | transient failures then success; retries exhausted → failed handover, dead letter, replay, handed over; rejected → dead letter; timeout → `409` → replay; wrong token → paused without dead letters; the circuit opening and closing |

The README maps each assignment requirement to its tests.

**Determinism**
- **No `sleep` in test code.** One helper, `eventually(condition, timeout=30 s)`, polls every 100 ms for a state: an order's `handover_status`, the stub's journal of fulfilment orders, a dead letter for an order id, a heartbeat showing `paused`. On timeout it fails with the last state it observed.
- **Order of start-up:** in-process tests publish first and then start the consumer (which reads from the earliest offset), so there is no race between assignment and publishing. Container tests wait until both heartbeats show assigned partitions.
- **Time in unit tests is fake**, and the jitter source is injectable, so backoff and the flood guard are exact.
- **No automatic re-runs** of failed tests: a flaky test is a bug.
- **Serial by default.** Container tests share the real consumers and must be serial; in-process tests could run in parallel thanks to their own topics, if that is ever needed.

**Compose** (`task2/compose.yaml`; image tags pinned exactly, current versions confirmed when building)

| Service | Image | Role |
|---|---|---|
| `kafka` | `apache/kafka:4.3.1` | single-node KRaft; `INTERNAL://kafka:9092` for containers, `EXTERNAL://localhost:9094` for the host; automatic topic creation off; named volume |
| `kafka-init` | `apache/kafka:4.3.1` (the JVM image, which has the CLI tools) | creates `orders` (3 partitions, 7 days) and `orders.dlt` (3 partitions, 30 days) if missing, then exits |
| `postgres` | PostgreSQL, current major version | named volume; an init script creates a migration owner and a consumer role with data rights only |
| `migrate` | dbmate | applies the migrations and the consumer role's grants, then exits |
| `fulfilment-stub` | built from `fulfilment-stub/` | the stand-in fulfilment service |
| `consumer` | built from `consumer/` | `replicas: 2`, `restart: unless-stopped`, `stop_grace_period: 20s`, heartbeat healthcheck; starts once Kafka is healthy, both init services have completed and the stub is healthy |
| `kafka-ui` | kafbat `kafka-ui:v1.5.0` | read-only, so the UI can't alter topics |
| `jaeger` | Jaeger all-in-one | only with the `tracing` profile |

- **Host ports:** kafka-ui 8080, Jaeger 16686, Kafka 9094, PostgreSQL 5432, stub 8081.
- **Fake secrets are committed** in the Compose file (the fulfilment token, database passwords).
- **Named volumes** keep data across `make down` and `make up`; `make clean` removes them.

**`make` targets**

| Target | Does |
|---|---|
| `up`, `up-tracing` | build, start and wait until healthy, then print the URLs |
| `down`, `clean` | stop, keeping data; stop and delete the volumes |
| `demo` | publish sample events (valid, invalid, redelivered, re-emitted, conflicting, failing downstream); show the outcome counts from terminal log lines and the dead letters; clear a fault and replay; point at kafka-ui and Jaeger |
| `publish ARGS=…` | publish sample events with a `traceparent` |
| `replay ARGS=…` | the replay command (ADR 0006), a dry run unless `--execute` |
| `test`, `test-unit`, `test-integration`, `test-reliability`, `lint` | the suites; `test` runs all of them |
| `logs`, `ps`, `psql` | follow logs, show health, open a SQL shell |

**CI** (`.github/workflows/task2-ci.yml`, on every push and pull request touching `task2/`)

| Job | Runs |
|---|---|
| static | `make lint`, plus actionlint for the workflow |
| unit | consumer and stub unit tests; coverage of `domain` and `application` must reach 90 % of lines, with branch coverage measured |
| integration | `make up`, `make test-integration`, `make test-reliability`, then `make demo` as a smoke test; uploads test reports, and on failure the Compose logs and heartbeat files; 25-minute timeout |

- **The coverage gate covers the core only.** That is where coverage is meaningful; adapters are proven by the integration tests against real services.
- **`make demo` runs in CI**, so the reviewer's first command can't break unnoticed.
- **CI runs on amd64**, development on arm64, so both architectures are exercised.
- **Artifacts are checked for secrets** (the token and the database passwords) before upload. The values are fake; the check proves the logging rules of ADR 0008 also hold in what leaves the runner.

## Considered options

- **Testcontainers for the integration tests.** Self-contained per test session, but its Kafka module accepts only Confluent's `cp-kafka` image, so tests would run against a different broker than the one shipped. The stub, migrations, topic creation and consumer containers would need hand-built containers, and its resource reaper is known to race when tests run in parallel.
- **Running pytest inside the Compose network:** container-to-container networking for tests, but slower to iterate and awkward for killing and scaling sibling containers.
- **Fixed waits (`sleep`):** either slow or flaky, and a failure says nothing about what went wrong.
- **Re-running failed tests automatically:** hides flakiness instead of fixing it.
- **A coverage gate on everything:** rewards testing adapters against fakes, which proves little.
- **CI only on the default branch, or without the demo:** problems would surface late, and the demo could rot.

## Consequences

- **Every test-relevant timing is configuration**, so `compose.test.yaml` and in-process tests can shorten it.
- **Topic names are configuration**, so in-process tests can use their own.
- **The repository's existing CI workflow runs on every push**, including changes to task2 only; worth a path filter when the work moves to its final repository.
- **What local tests can't prove:** behaviour with several brokers (leader changes, under-replicated partitions) and under load. Named in the production-evolution section.
