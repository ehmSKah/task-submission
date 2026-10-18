---
status: accepted
---

# Observability: W3C Trace Context as the correlation id, OpenTelemetry traces, structured logs on stdout

The correlation id is the **trace id of a W3C Trace Context `traceparent` header**, the standard OpenTelemetry is built on, so any instrumented producer or fulfilment service joins the same trace without extra work. The consumer and the stub are traced with the **OpenTelemetry SDK**, and **Jaeger** can be started alongside to see each order's trace. Logs are **one JSON line per event on stdout**, written with `structlog` under **fixed event names**, in the shape of OpenTelemetry's log model. **Metrics are not built.** Every failure can be followed end to end from four places linked by one id. Terms are defined in [`CONTEXT.md`](../../CONTEXT.md).

## Decision

**The correlation id**

| Stage | Rule |
|---|---|
| Source | the trace id of the record's `traceparent` header |
| Missing or invalid header | a new trace is started; a header longer than 128 characters or not in the W3C format is replaced, never logged as it is (it could be crafted to inject log lines); `trace_source` records `header`, `new` or `replaced`; the event id is logged next to it |
| Within the consumer | bound once per message, so every log line and span for that message carries it |
| Handover | sent as `traceparent` on every attempt; the stub continues the trace and logs the same trace id |
| Stored | `orders.trace_id` and `processed_events.trace_id` (ADR 0002) |
| Dead letters | original headers copied; if the consumer started a new trace, its `traceparent` is set (ADR 0006) |
| Replay | headers are kept, so a replayed dead letter continues its original trace |

**Traces** (OpenTelemetry SDK)
- **One span per message** (`process orders`), with child spans for the database write and **each handover attempt** (`httpx` instrumentation). Retries appear as sibling attempt spans with their status and duration; the stub's span sits inside each attempt.
- **Span names and attributes follow OpenTelemetry's messaging and HTTP conventions** (topic, partition, offset, consumer group; method, status code).
- **The exporter is configuration** (OTLP endpoint). With none configured, spans are created and discarded at negligible cost.
- **Jaeger runs in an optional Compose profile** (`make up-tracing`); it accepts OTLP directly, so no collector is needed locally.

**Logs**
- **One JSON object per line on stdout**, collected by Docker. Consumer and stub use the same setup.
- **`structlog`** with the event name first (`log.info("handover.attempt_failed", attempt=2, status=503)`), context bound once per message, and a processor that adds `trace_id` and `span_id` from the current span. Library logs (`httpx`, `uvicorn`) go through the same renderer.
- **Levels:** `INFO` for the normal flow, `WARNING` for retries, dead letters and pauses, `ERROR` for unexpected errors, failed dead-letter writes and failed commits. `DEBUG` through an environment variable.

**Fields**

| Always | Per message, once known | Per handover attempt |
|---|---|---|
| `timestamp` (UTC), `level`, `event`, `service` (`order-consumer`, `fulfilment-stub`), `instance` | `trace_id`, `span_id`, `trace_source`, `topic`, `partition`, `offset`, `event_id`, `order_id` | `attempt`, `max_attempts`, `response_class` (`success`, `transient`, `rejected`, `systemic`), `status`, `duration_ms` |

**Events** (fixed names, part of the contract)

| Area | Events |
|---|---|
| Lifecycle | `consumer.started`, `consumer.configuration_invalid` (reason), `consumer.stopping`, `consumer.stopped` (exit code), `partitions.assigned`, `partitions.revoked`, `partitions.lost`, `message.dropped`, `commit.failed`, `kafka.error` (a non-fatal client error, with its reason) |
| Processing | `event.received`, `event.invalid` (layer, error count), `order.accepted`, `order.duplicate` (redelivery or re-emission), `order.conflict` |
| Handover | `handover.unfinished` (the order was already stored but not handed over, with its `handover_status`; this event completes it), `handover.attempt_failed`, `handover.completed`, `handover.failed` (retries exhausted or rejected) |
| Dead letters | `dead_letter.written` (reason), `dead_letter.write_failed` |
| Flood guard | `circuit.opened`, `circuit.probe_failed`, `circuit.closed`, `bug_guard.tripped`, `consumer.paused`, `consumer.resumed` (with the systemic reason) |
| Errors | `processing.unexpected_error`, the only line with a stack trace |
| Fulfilment stub | `fulfilment_order.created`, `request.invalid` (the failing fields as JSON Pointers and the rule each broke, never their values) |

- **Exactly one terminal line per message:** `handover.completed`, `order.duplicate` or `dead_letter.written`. Outcomes can be counted straight from the logs, and the demo does so.
- **The heartbeat file's state** (ADR 0007) matches the last `consumer.paused` or `consumer.resumed`.

**Never logged, and never a span attribute**

| Data | Why |
|---|---|
| the bearer token, any `Authorization` header | a secret |
| database credentials, including connection strings with a password | a secret |
| event payloads, unreadable raw bytes, request and response bodies | size and unknown content; the dead letter keeps the original bytes |
| `customer_id` | a pseudonymous reference is still personal data under the GDPR; the order id finds everything |
| full values inside validation messages | validation messages quote the offending value; values are cut to 64 characters, in logs and in `dlt.detail` |

- **By construction:** log calls pass named fields, never whole objects or bodies; the `httpx` instrumentation is not configured to record headers.
- **Defence in depth:** a `structlog` processor redacts fields whose names look sensitive (`authorization`, `token`, `password`, `secret`, `dsn`).
- **Tested:** a handover with a known token, asserting that it appears in no log line and no span attribute. The stub follows the same rules.

**Following one failure end to end**, given an order id:
1. **PostgreSQL:** `orders` shows `handover_status`, `trace_id` and the Kafka position; `processed_events` lists every event that arrived for the order.
2. **Jaeger** (with the profile): the trace shows each attempt with its status and duration, the stub's span inside it, and a replay as a later span in the same trace.
3. **Logs:** filtering consumer and stub logs by the trace id gives every line in order, from `event.received` to the terminal line.
4. **kafka-ui:** if the order was dead-lettered, `orders.dlt` shows the original record with `dlt.reason`, `dlt.detail` and `dlt.attempts`.

**Metrics: not built.** The assignment doesn't ask for them, and the terminal-line rule gives outcome counts locally. A metrics backend would bring the Collector, Prometheus and Grafana into the local stack, and a `/metrics` endpoint would need a second thread in a deliberately single-threaded consumer.

## Considered options

- **Correlation id.**
  - *A custom `correlation-id` header:* simple, but a convention of our own that no other system understands.
  - *W3C Trace Context without the SDK* (propagating `traceparent` by hand): standard ids everywhere, but nothing to see them in.
- **Tracing scope.**
  - *Full OpenTelemetry* (traces, metrics and logs through a Collector, with Prometheus and Grafana): three or four more containers and dashboards, beyond the assignment.
- **Logging library.**
  - *The standard `logging` module with a JSON formatter:* no new dependency, but message-first, so fixed event names depend on discipline, and per-message context needs a custom filter.
  - *Exporting logs over OTLP from the process* (OpenTelemetry Logs SDK): one pipeline for traces and logs, but it couples logging to a collector being reachable, and records buffered in memory are lost during exactly the incidents they describe. There is also no local logs backend: Jaeger stores traces only. Stdout keeps OpenTelemetry compatibility anyway, because a collector can read container logs and map these fields to OpenTelemetry log records without code changes.
- **Logging `customer_id`:** helpful for support, but personal data the order id makes unnecessary.

## Consequences

- **Dependencies:** `opentelemetry-sdk`, the OTLP exporter and the `httpx` instrumentation for both services, plus `structlog`.
- **Compose:** an optional `tracing` profile with Jaeger; consumer and stub read the OTLP endpoint from the environment.
- **Tests:** the token-leak test; one terminal line per message; trace continuity across a handover, a dead letter and its replay.
- **Production evolution:** the OpenTelemetry Collector reading container logs and exporting logs and traces; metrics (consumer lag from a Kafka exporter; outcomes, attempts, latency, circuit state and dead-letter rate from the OpenTelemetry metrics SDK) with alerts on lag, an open circuit and dead-letter growth; sampling for traces at volume.
