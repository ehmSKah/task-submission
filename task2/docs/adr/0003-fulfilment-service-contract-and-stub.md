---
status: accepted
---

# Fulfilment service: an idempotent `POST`, four response classes, and a stub we own

The assignment requires a downstream REST API but leaves its contract open. We define one: a **fulfilment service** that ships accepted orders. Each order is handed over with one `POST` carrying the order id as **`Idempotency-Key`**, so repeating a handover never creates a second fulfilment order. Every response falls into one of four classes, which drive the retry strategy. One handover attempt has fixed timeouts, and **only one layer retries**. The fulfilment service is played by **our own small stub**, which honours the idempotency key, injects faults per order and keeps a journal, so tests can prove "exactly one fulfilment order per order". Terms are defined in [`CONTEXT.md`](../../CONTEXT.md).

## Decision

**Contract** (`task2/openapi/fulfilment.yaml`)
```http
POST /fulfilment-orders
Authorization: Bearer <token>
Content-Type: application/json
Idempotency-Key: 3f1c…            # the order id (ADR 0002)
traceparent: 00-…               # W3C Trace Context, the correlation id (ADR 0008)

{
  "externalReference": "3f1c…",
  "customerReference": "a9e2…",
  "orderedAt": "2026-10-17T09:12:03Z",
  "lines": [
    { "sku": "KNE-ESC-0042", "quantity": 2,
      "unitPrice": { "amount": "19.99", "currency": "EUR" },
      "lineTotal": { "amount": "39.98", "currency": "EUR" } }
  ],
  "total": { "amount": "39.98", "currency": "EUR" }
}
```
- **The body is the downstream request**, produced from our Order by mapping 2 (ADR 0001). Money becomes a decimal string using the currency's exponent.
- **`201 Created`** returns `{ "id": "ful-8812", "externalReference": "3f1c…" }`. The fulfilment order id is stored with the order as proof that the handover is complete.
- **Replays** follow the IETF draft *The Idempotency-Key HTTP Header Field*:

| Same `Idempotency-Key`, and … | Response |
|---|---|
| the same body | the original `201`, same fulfilment order id; nothing new is created |
| a different body | `422`. This would be our bug: conflicting events are dead-lettered before any handover (ADR 0002). |
| the first request is still in progress | `409` |

- **The README states** that the fulfilment service is a stand-in designed for this assignment.

**Response classes**

| Class | Responses | Meaning |
|---|---|---|
| **Success** | `201`, including a replay | the handover is complete |
| **Transient** | connection refused or reset, connect or read timeout, `408`, `429`, `500`, `502`, `503`, `504`, `409` in progress, any other `5xx` | may succeed later; `Retry-After` is honoured, up to a cap |
| **Rejected** | `400`, `413`, `422`, any other `4xx` not listed as systemic | this order will never be accepted as sent; it goes to the dead-letter topic without retries |
| **Systemic** | `401`, `403`, `404`, `405`, `415`, any `3xx`, any `2xx` other than `201` | our configuration or the service is wrong for *every* order; consumption stops without committing and nothing is dead-lettered |

- **`409` in progress is transient.** When our request times out but the fulfilment service is still working on it, the retry gets `409` and a later one gets the `201` replay: the handover completes exactly once. This is a named test.
- **Systemic failures are not dead-lettered.** With a wrong URL or token every order fails the same way. Dead-lettering them would fill the dead-letter topic with healthy orders that all need replaying. Not committing leaves the backlog safely in Kafka until the configuration is fixed. How consumption stops (pausing or exiting) is decided with the retry and restart strategies.

**One handover attempt**

| Setting | Default |
|---|---|
| Connect timeout | 2 s |
| Read timeout | 5 s |
| Deadline for the whole attempt | 10 s |
| Redirects | not followed (a `3xx` is systemic) |
| Retries inside the HTTP client | off |
| Connections | one keep-alive pool per consumer process |

- **Only one layer retries: the consumer's retry policy.** If the HTTP client retried as well, the bound would multiply (3 × 3 = 9 attempts instead of 3). Many clients retry by default, so this is set explicitly.
- **Kafka sets the outer limit.** A consumer that doesn't poll for longer than `max.poll.interval.ms` (300 s by default) loses its partitions and the message is redelivered. With at most 10 s per attempt, the retry strategy can work out how many attempts and how much backoff fit between polls, or pause the partition instead.
- **Every value is configuration** with these defaults, so tests can shorten them (`FULFILMENT_CONNECT_TIMEOUT_SECONDS`, `FULFILMENT_READ_TIMEOUT_SECONDS`, which also bounds writing the request, and `FULFILMENT_ATTEMPT_DEADLINE_SECONDS`).
- **The deadline covers the whole attempt.** `httpx`'s timeouts bound each phase, not their sum, so the attempt runs in a thread of its own and is abandoned when its deadline passes. An abandoned request still ends within its phase timeouts; whatever it achieved, the idempotency key makes the next attempt safe.
- **The HTTP adapter sorts every answer into its class**, including timeouts and dropped connections; the core sees only the class, the status and a short error. A different contract changes the adapter, not the retry policy.
- **Library:** `httpx`, which has sync and async APIs and timeouts per phase; `respx` mocks it in unit tests.

**Authentication**
- **A static bearer token** from `FULFILMENT_TOKEN`. The committed Compose file sets an obviously fake value (`local-dev-token`), which both the consumer and the stub read.
- **The token is never logged.**
- **A wrong-token test** proves the systemic path: a `401` stops consumption without dead-lettering anything.
- **Authentication sits in the HTTP adapter**, so replacing it touches nothing else.

**The stub** (`fulfilment-stub`, a small FastAPI service in Compose)
- **Contract side:**
  - It implements `POST /fulfilment-orders` exactly as specified.
  - It validates requests strictly against the OpenAPI file, so a `400` from the stub means our mapping produced something wrong.
  - It remembers each `Idempotency-Key` with a fingerprint of the body and applies the replay rules above.
  - State is in memory: restarting the stub forgets keys. The restart tests restart the consumer, not the stub.
- **Control side** (`/_control/…`, not part of the contract):
  - **Fault rules** are matched per order (`externalReference`), so tests running side by side don't interfere. Each rule applies `times: N`. It can respond with a status (`503` with or without `Retry-After`, `429`, `500`, `400`, `404`, …), respond after a delay, **create the order but answer too late** (which reproduces timeout → `409` → replay), or drop the connection. Rules are added with `POST /_control/fault-rules` (`{"externalReference": …, "times": 1, "delaySeconds": 3}`); every request for the order that passes the contract checks uses one. A delayed request has already claimed its key, so a repeat meanwhile gets `409`.
  - **Journal:** every request received with its headers and outcome (no status yet while it is in progress, which is what the hard-kill test waits for), and separately the fulfilment orders actually created. Duplicate tests assert "exactly one fulfilment order for this order", however many requests arrived.
  - **Reset** clears rules, journal and keys between tests.
  - `make demo` uses the same fault rules.

## Considered options

- **Contract.**
  - *`PUT /fulfilment-orders/{externalReference}`:* idempotent by HTTP semantics, with no key header and no key store downstream; a conflicting body would get `409`. Rejected because it assumes the downstream lets clients choose resource ids, which external APIs rarely do, and it hides the idempotency mechanism the assignment asks to see.
- **Response classes.**
  - *Two classes (retryable and permanent):* a configuration error would send every order to the dead-letter topic.
  - *Retrying every non-`2xx`:* rejected orders would use up the retry budget for nothing.
- **Retries.**
  - *Retries in the HTTP client as well as in the consumer:* the bound multiplies and nobody owns it.
- **Stub.**
  - *WireMock:* fault injection, delays, scenarios and a request journal are built in. Idempotency per key (fingerprint, replay, `422`, `409` in progress) would need a Java extension, and it adds a JVM container.
  - *Prism (an OpenAPI mock):* validates against the spec but keeps no state, so it can't replay or count fulfilment orders.
  - *Mocks inside the tests only (`respx`):* no real HTTP hop end to end and nothing to show in the demo. Kept for unit tests of the HTTP client.
  - *Public services (httpbin, webhook.site):* non-deterministic, external, unusable in CI.
- **Authentication.**
  - *None:* the systemic class would have no realistic trigger.
  - *OAuth2 client credentials:* the usual production answer for service-to-service calls (token endpoint, caching, refresh on `401`). A second protocol with more failure modes than the assignment needs.
  - *Mutual TLS:* strong, but heavy to set up locally and to explain.

## Consequences

- **The order record gains the fulfilment order id and the time of handover.** The exact columns, and how an unfinished handover is recognised, are decided with the dual-write strategy.
- **The retry strategy builds on these classes:** transient failures get bounded retries within the poll-interval budget, rejected orders go to the dead-letter topic, and systemic failures stop consumption without committing.
- **Production evolution:** OAuth2 client credentials or mutual TLS instead of the static token; a circuit breaker in front of the fulfilment service; the real service's contract replacing ours, with only mapping 2 and the HTTP adapter changing.
