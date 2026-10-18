---
status: accepted
---

# Local stack and testing: one Compose stack for reviewers, tests and CI

The **Compose stack is the deliverable**, so it is also what the tests run against: the same broker image, the same services, the same configuration apart from shorter timeouts. pytest runs on the host. Most integration tests run the consumer **in the test process** against the stack's Kafka, PostgreSQL and stub; the lifecycle scenarios drive the **real consumer containers**. The assignment's four test categories each have **one file named after them**. No test sleeps: every wait is for a state, and a failed wait reports what it saw. CI runs everything, including the demo, on every change to task2.

## Decision

**Harness**
- **`docker compose up --wait`** for the stack. The reliability suite layers `compose.test.yaml` on top for the consumer service only: it recreates the consumers with it before its tests and with the stack's own settings afterwards, so `make up` and the reviewer's stack are untouched. The override shortens the session timeout (6 s, for the zombie test) and the probe interval (5 s). Each container test leaves the stack as it found it: whatever it stopped, froze or scaled down runs again, healthy.
- **In-process consumer tests.** pytest runs the consumer inside the test process against the stack's Kafka, PostgreSQL and stub. Each test uses its own topics (`orders.test.<id>`, `orders.test.<id>.dlt`) and consumer group, so the stack's consumers never take its messages, and a new group reading from the earliest offset never sees another test's events. The crash-point wrapper of ADR 0007 runs this way.
- **Container tests.** These kill, stop and scale the real consumer containers on the real `orders` topic.
- **Isolation:** every test uses fresh order ids, the stub's fault rules match per order (ADR 0003), and assertions look only at the test's own orders. No test depends on an empty database.

**Suites**

| Suite | Where | Runs against | Proves |
|---|---|---|---|
| Unit | `consumer/tests/unit` | plain Python with fakes behind every port and a fake clock | each validation layer and business rule; both mappings, with property tests (the total always equals the sum of line totals; money formats correctly for every currency exponent); duplicate versus conflict; retry policy and backoff; the flood guard's states; the outcome-to-commit table; the start-up budget check; log redaction |
| Stub unit | `fulfilment-stub/tests` | the stub in the test process | the stored response, `422`, `409` in progress, fault rules, the journal |
| Demo unit | `demo/tests` | plain Python with a fake stack | the sample events, against the consumer's own validation; outcome counting from log lines; the runner stopping at the first unmet outcome; the sections matching the four category files and `make demo-tests` |
| Integration | `tests/integration` | the in-process consumer against the stack | the four assignment categories; crash points; dead-letter contents; replay; trace continuity (OpenTelemetry's in-memory span exporter); no token in logs or spans; one terminal log line per message |
| Reliability | `tests/reliability` | the consumer containers | hard kill mid-handover, graceful stop mid-stream, rebalance under load, zombie consumer, pause during a rebalance (ADR 0007) |
| Static | the repository | | ruff, mypy (strict), import-linter; the JSON Schema and OpenAPI files are valid and their examples pass |

**The assignment's four categories**, one integration file each:

| File | Cases |
|---|---|
| `test_successful_processing.py` | order stored and handed over; fields and money mapped; one terminal log line; one trace across the handover |
| `test_invalid_messages.py` | one case per validation layer: unreadable, not a CloudEvent, unsupported type or version, schema violation, business rule. Each ends as a dead letter naming the layer and errors, with nothing stored and nothing handed over |
| `test_duplicate_events.py` | redelivery, re-emission, a conflicting event (dead letter), two consumers racing on one order; in every case exactly one fulfilment order |
| `test_downstream_failures.py` | transient failures then success; retries exhausted → failed handover, dead letter, replay, handed over; rejected → dead letter; timeout → `409` → stored response; wrong token → paused without dead letters; the circuit opening and closing |

Replay has a file of its own, `test_replay.py`: a `retries-exhausted` dead letter replayed once the fault is cleared ends handed over in its trace; a dry run changes nothing; a second replay creates no second fulfilment order; invalid and conflicting dead letters are skipped unless forced.

The README maps each assignment requirement to its tests.

**The demo** (`demo/`, run on the host like pytest, with `uv`)
- **`make demo` shows the four categories in four sections named after them**, each ending with the file above that proves it: three valid orders handed over (EUR, JPY and KWD, showing the money mapping from what the stub received); one invalid event per validation layer; a redelivery, a re-emission and a conflicting event; two `503`s then success, and an order that always fails, is dead-lettered as `retries-exhausted`, has its fault rule cleared and is replayed with the replay command until it is handed over in its original trace (the replay step also points at `test_replay.py`, which proves the command on its own; `make demo-tests` stays the four category files). A **summary** counts the outcomes from the terminal log lines of the run's traces (ADR 0008), lists the run's dead letters by reason and points at kafka-ui, Jaeger (the replayed order's trace, when `make up-tracing` started it), `make psql` and `make ps`.
- **It fails at the first outcome that isn't the one promised**, printing what it last observed, so it is also CI's smoke test. Like the tests, it never sleeps for a fixed time; it waits for states.
- **Every run publishes new orders, each record with a `traceparent` of its own**, and looks only at those orders and traces, so it runs again on the same stack without `make clean`. It takes under half a minute.
- **It runs on the stack's own settings, without a demo override.** The only timing it meets is the backoff of the order that runs out of retries, about 8 s (at most 15 s); recreating the consumers with shorter timings would cost about 10 s, twice if they were put back afterwards, and would leave a stack unlike the one `make up` starts.
- **`make publish`** sends the same sample events one kind at a time (`ARGS="re-emitted --count 3"`), printing each record's order, event and trace ids and its position, never a body or a customer id.
- **`make demo-tests`** runs exactly the four category files.

**Determinism**
- **No `sleep` in test code.** One helper, `eventually(condition, timeout=30 s)`, polls every 100 ms for a state: an order's `handover_status`, the stub's journal of fulfilment orders, a dead letter for an order id, a heartbeat showing `paused`. On timeout it fails with the last state it observed.
- **Order of start-up:** in-process tests publish first and then start the consumer (which reads from the earliest offset), so there is no race between assignment and publishing. Container tests wait until both heartbeats show assigned partitions.
- **Time in unit tests is fake**, and the jitter source is injectable, so backoff and the flood guard are exact.
- **Integration tests may pin the jitter** where a scenario depends on when attempts happen: timeout → `409` in progress → stored response needs one attempt inside the stub's delay and a later one after it. Everywhere else the jitter stays random, within its bounds.
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
| `demo` | the four categories in four sections (see *The demo*): publish sample events (valid, invalid, redelivered, re-emitted, conflicting, failing downstream); clear a fault and replay; show the outcome counts from terminal log lines and the dead letters; point at kafka-ui and Jaeger |
| `demo-tests` | the four category test files |
| `publish ARGS=…` | publish sample events of one or more kinds, each with a `traceparent` of its own |
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
- **A demo override shortening the retry backoff** (a `compose.demo.yaml` like the reliability suite's): saves about 7 s of backoff, but recreating the consumers costs more than that, and leaving it in place would make the reviewer's stack differ from `make up`'s and drop tracing unless Jaeger is detected.
- **The demo as a shell script, or inside the consumer image:** a script can't check what it shows as readably, and the image would need Docker and the stub's control side to run it; on the host it reaches the stack the way the tests do.
- **Re-running failed tests automatically:** hides flakiness instead of fixing it.
- **A coverage gate on everything:** rewards testing adapters against fakes, which proves little.
- **CI only on the default branch, or without the demo:** problems would surface late, and the demo could rot.

## Consequences

- **Every test-relevant timing is configuration**, so `compose.test.yaml` and in-process tests can shorten it.
- **Topic names are configuration**, so in-process tests can use their own.
- **The repository's existing CI workflow runs on every push**, including changes to task2 only; worth a path filter when the work moves to its final repository.
- **What local tests can't prove:** behaviour with several brokers (leader changes, under-replicated partitions) and under load. Named in the production-evolution section.
