---
status: accepted
---

# Stack and code structure: Python with our own poll loop, synchronous, hexagonal

The consumer is written in **Python 3.13** on **`confluent-kafka`**, with **our own poll loop** rather than a framework. The assignment asks to demonstrate the reliability mechanics (committing, retrying, dead-lettering), and in Python the frameworks that exist hide exactly those, so writing the loop ourselves is the only way to keep the guarantees of ADR 0004. The code is **synchronous**, processes one message at a time, and is organised as **ports and adapters**. Each contract has **one definition** (a JSON Schema for the event, an OpenAPI file for the fulfilment service), and code validates against it rather than describing it again.

## Decision

**Language and client**
- **Python 3.13 + `confluent-kafka` 2.16** (librdkafka underneath, the same C client many production consumers use). Typed, with Python 3.13 wheels for amd64 and arm64.
- **Our own poll loop.** Every commit, retry and dead letter is our code, visible and tested.
- **The same toolchain as task1:** uv, ruff, mypy (strict), pytest, import-linter, plus hypothesis for property tests of validation and the mappings.

**Synchronous, one message at a time.** A blocking loop polls, processes and commits. With one message in hand at a time (ADR 0004), asyncio would add no concurrency.

**Libraries**

| Concern | Choice |
|---|---|
| Kafka | `confluent-kafka` |
| Database | `psycopg` 3, synchronous; one connection per process with reconnect; plain SQL |
| HTTP | `httpx`, synchronous; `respx` in unit tests |
| Event validation (layers 2–4 of ADR 0001) | `jsonschema` against `order-placed.v1.schema.json`, collecting every error with its path |
| Domain model | frozen dataclasses (`Order`, `OrderLine`, `DownstreamRequest`), built by the two pure mappings |
| Business rules (layer 5) | plain Python functions |
| Configuration | a frozen `Settings` dataclass read from the environment, failing fast on start-up |
| Fulfilment stub | FastAPI + uvicorn; requests checked with `openapi-core` against `fulfilment.yaml` |

**One definition per contract.** The event's rules live in its JSON Schema file and the fulfilment API's in its OpenAPI file. Neither is described a second time in code (no Pydantic models mirroring them), so they can't drift apart.

**Process model**
- **One consumer per process, one process per container.**
- **Compose runs two consumer instances** in one consumer group against an `orders` topic with **three partitions**, so the partitions are visibly shared (2 + 1) and a rebalance can be watched when one instance stops.

**Code structure**
```
task2/
├── schemas/order-placed.v1.schema.json     # the event contract (ADR 0001)
├── openapi/fulfilment.yaml                 # the fulfilment contract (ADR 0003)
├── db/migrations/                          # plain SQL, dbmate (ADR 0002)
├── consumer/                               # uv project → consumer image
│   └── src/order_consumer/
│       ├── domain/        # Order, OrderLine, business rules, mappings 1 and 2
│       ├── application/   # processing one event (the steps of ADR 0004) → an Outcome
│       │                  # ports: OrderStore, FulfilmentClient, DeadLetterSink, Clock
│       ├── adapters/
│       │   ├── inbound/   # the poll loop: poll → process → commit according to the Outcome
│       │   └── outbound/  # Postgres store, fulfilment HTTP client, Kafka dead-letter producer
│       ├── config.py
│       └── main.py
├── fulfilment-stub/                        # separate uv project → stub image
└── compose.yaml, Makefile, README.md, docs/
```
- **Dependency direction:** adapters → application → domain, never the other way.
- **Processing an event returns an `Outcome`** (handed over, duplicate, dead-lettered, systemic failure), and **only the poll loop commits**, following the table in ADR 0004. Commit timing lives in one place, and all processing logic can be tested without Kafka.
- **Ports only where a second implementation is real:** test fakes for the store, the fulfilment client and the dead-letter sink, and a clock so retry backoff can be tested without sleeping.
- **Enforced:** an import-linter contract in CI fails the build if `domain` or `application` import `confluent_kafka`, `psycopg` or `httpx`.
- **The stub is a separate project.** It stands in for another team's system, so it shares no code with the consumer, and the consumer image doesn't ship FastAPI.

## Considered options

- **Java or Kotlin with Spring Kafka.** The industry standard: an error handler with backoff, a dead-letter recoverer and non-blocking retry topics come built in and well tested. Not chosen because those mechanics would become configuration, while the assessment asks to show them in code, and because it would add a second stack next to task1 in a language that isn't the author's strongest.
- **Python frameworks.**
  - *FastStream:* its confluent broker commits automatically by default, and its `ack()` commits the whole last poll asynchronously, which contradicts committing each message only after its outcome.
  - *Quix Streams:* pinned to an older `confluent-kafka`, and built for stream processing with state, which we don't need.
  - *faust-streaming:* skips a message when the handler raises, losing it.
  - None of them offers bounded retries together with a dead-letter topic.
- **Go** (`franz-go`) **or Node:** a third stack; Node's Kafka clients are thinner for the features assessed.
- **asyncio.** `confluent-kafka`'s async consumer and producer run the synchronous client on a thread pool, so nothing is gained underneath, and a teardown crash with `confluent-kafka` and asyncio under Testcontainers is known. Worth revisiting only for processing several orders of a partition in parallel, where threads are equally valid.
- **Pydantic** for the event, the domain model or settings: popular, but a second definition of rules the schema files already hold.
- **SQLAlchemy** (ORM or Core): unnecessary for a handful of hand-written statements; migrations are plain SQL anyway.
- **A connection pool:** one message at a time needs one connection.
- **One project for consumer and stub:** simpler packaging, but it blurs the line between our system and the stand-in for someone else's.

## Consequences

- **The reliability layer is ours to write and test**: the poll loop, commits, retries and dead-lettering.
- **Throughput is one message at a time per instance**; scaling means more partitions and instances. Processing several orders of a partition in parallel is a production evolution that needs offsets tracked per key.
- **Two images:** the consumer and the fulfilment stub.
