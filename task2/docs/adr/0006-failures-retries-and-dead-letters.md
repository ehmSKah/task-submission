---
status: accepted
---

# Failures, retries and dead letters: four classes, blocking retries, a flood guard, one dead-letter topic

Every failure falls into one of **four classes**, and the class alone decides what happens. **Transient** failures are retried in the consumer, a bounded number of times, within a budget the consumer checks against Kafka's poll interval when it starts. A **flood guard** recognises when failures stop being about individual messages (an outage, a bug) and pauses consumption instead of dead-lettering healthy orders. The rest goes to **one dead-letter topic**, as the original record plus headers that say what went wrong, and a small command can **replay** it once the cause is fixed. Terms are defined in [`CONTEXT.md`](../../CONTEXT.md).

## Decision

**Failure classes**

| Class | Failures | Handling | Offset |
|---|---|---|---|
| **Permanent** | invalid event (ADR 0001); conflicting event (ADR 0002); handover rejected (ADR 0003) | dead-lettered at once, no retries; a rejected handover sets `handover_failed` first | committed once the dead letter is confirmed |
| **Transient** | fulfilment connection errors, timeouts, `408`, `429`, `5xx`, `409` in progress | bounded retries; if they run out: `handover_failed`, dead-lettered as `retries-exhausted` | committed once the dead letter is confirmed |
| **Systemic** | fulfilment `401`, `403`, `404`, `405`, `415`, `3xx`, unexpected `2xx`; PostgreSQL unavailable (after 3 quick reconnects within about 5 s); the dead-letter topic can't be written | consumption **stops** (see *Stopping*); nothing is dead-lettered | not committed |
| **Unexpected** | any other exception: a bug for this message | dead-lettered at once with the exception type; the stack trace goes to the error log | committed once the dead letter is confirmed |

- **A unique-key violation from a racing consumer is not a failure.** It is the duplicate path of ADR 0002, handled in code.
- **A violated database CHECK constraint is unexpected**: validation should have caught it.
- **Unexpected exceptions are dead-lettered, not crashed on.** A crash would restart onto the same message and loop forever, blocking the partition (a poison pill).
- **If the dead-letter topic can't be written, nothing can be committed**, so the only safe move is to stop.

**Retries: blocking, in the consumer**
- **The message stays in hand** while the consumer waits and retries; it does not poll in between. Order is kept and the mechanism stays simple.
- **Bounds:**

| Setting | Default |
|---|---|
| Attempts per handover | 5 (the first and 4 retries) |
| Backoff | exponential with full jitter: random between 0 and min(15 s, 1 s × 2ⁿ) |
| One attempt | at most 10 s (ADR 0003) |
| Worst case | ≤ 15 s of waiting + ≤ 50 s of attempts = **≤ 65 s**, under a quarter of the 300 s poll interval |
| `Retry-After` | honoured up to 15 s; a longer one stops the retries at once (`retries-exhausted`, noting the value) |

- **Full jitter** spreads the retries of the two consumer instances, so they don't hit a recovering fulfilment service at the same moment.
- **A long `Retry-After` ends the retries** rather than being shortened: the service has said it is unavailable for a while, and that is the flood guard's business.
- **The budget is enforced when the consumer starts.** It refuses to start if attempts × attempt deadline + backoff caps exceed half of `max.poll.interval.ms`, so a configuration change can't silently break the poll budget.
- **Accepted cost: head-of-line blocking.** While one order is being retried, the other partitions of that consumer wait, for at most about a minute.
- **Backoff goes through the `Clock` port** (ADR 0005), so it is tested without sleeping.

**The flood guard**

An outage is not a property of the message. Without a guard, a 30-minute fulfilment outage would spend 65 s on every order and then dead-letter it: dozens of healthy orders in the dead-letter topic, each needing a replay, and each replay widening the key-memory gap of ADR 0004.
- **Circuit breaker:** after **3 consecutive orders** run out of retries, the circuit opens. The order in hand is neither dead-lettered nor committed, and consumption stops. Every **30 s** that order is tried once more; when it succeeds, the circuit closes and the backlog is processed in order. The orders dead-lettered before the circuit opened stay in the dead-letter topic for replay.
- **Bug guard:** after **5 consecutive `unexpected`** dead letters, consumption stops in the same way. A bug that fails every message should wait in Kafka for a fix, not empty the topic into the dead-letter topic.
- **Invalid and conflicting events never trip the guard.** A producer sending bad data should fill the dead-letter topic; stopping would hold up the valid events behind them.
- **Thresholds and the probe interval are configuration.**

| | Without the guard | With the guard |
|---|---|---|
| During a 30-minute outage | every order uses up its retries and is dead-lettered | after about 3 minutes the instance pauses; the backlog waits in Kafka |
| When the service is back | dead-lettered orders wait for someone to replay them | processing resumes within 30 s, in order, with nobody involved |
| The dead-letter topic holds | bad data mixed with outage casualties | bad data, plus at most two orders from the outage |

**Stopping** (for every systemic cause and for the flood guard)
- **Pause in place and probe.** The instance pauses its partitions and **keeps polling**, so it stays in the group even when the outage outlasts `max.poll.interval.ms`; every 30 s it tries the order in hand again.
- **Per instance.** There is no coordination across the group. Shared causes (an outage, a wrong token, the database or the dead-letter topic down) trip every instance on its own within a minute or two.
- **Optional exit after N minutes**, off by default locally. A paused instance keeps its partitions, which is right for shared causes but holds them back when the problem is local to one instance. Exiting hands its partitions to a healthy instance; this is the production setting where instances run on different hosts.
- **Kafka keeps the backlog.** Producers keep writing and consumer lag grows. The limit is the topic's retention: an outage longer than that loses the oldest unread events (ADR 0004). Lag and the circuit state are what production monitoring alerts on.

**Beyond 300 seconds.** Blocking retries must stay within the poll interval. If a single order ever needs to wait longer:

| Option | How | When |
|---|---|---|
| Retry topics | publish to `orders.retry.1` (10 s), `.2` (1 min), `.3` (10 min), then the dead-letter topic; commit the original | per-order transient failures become common, or backoffs of minutes are needed; removes head-of-line blocking |
| Pause and keep polling | pause the partition during each backoff and keep polling | longer backoffs without new topics; must handle losing the partition in a rebalance |
| A longer `max.poll.interval.ms` | raise the limit | the bluntest option; it also delays noticing a consumer that is really stuck |
| Transactional outbox | retries move out of Kafka into a relay (ADR 0004) | the downstream is slow or down often enough to hold up consumption |

**The dead-letter topic**
- **One topic, `orders.dlt`**, for every reason. Topic names use dots only (mixing `.` and `_` collides in Kafka's metric names).
- **3 partitions, keyed by order id** like `orders`, so dead letters for one order stay together and a replay can keep their order.
- **Retention 30 days**, longer than the 7 days of `orders`: a dead letter is a work item for a person and must outlive its source event.
- **No compaction.** Every dead letter is kept, including a second copy written by a crash.

**What a dead letter carries**
- **Value:** the original bytes, unchanged, even when unreadable.
- **Key:** the original key (the order id).
- **Original headers:** copied unchanged, including `correlation-id`.
- **Failure headers**, each with its own key (kafka-ui shows headers as a map, so a repeated key would hide values):

| Header | Content |
|---|---|
| `dlt.reason` | `invalid`, `conflict`, `rejected`, `retries-exhausted` or `unexpected` |
| `dlt.detail` | JSON depending on the reason: the validation layer and up to 20 errors with JSON Pointers; the fields that differ; the HTTP status and the start of the problem body; the last error and a long `Retry-After`; the exception type and message. Truncated to about 4 KB. |
| `dlt.attempts` | handover attempts made |
| `dlt.source` | topic, partition and offset, e.g. `orders/2/1187` |
| `dlt.source-timestamp`, `dlt.failed-at` | the original record's time and when it was dead-lettered (UTC) |
| `dlt.event-id`, `dlt.order-id` | when they could be read |
| `dlt.consumer` | consumer group and instance |

- **Never in a dead letter:** the bearer token and stack traces. Stack traces are in the error log, found through the correlation id.
- **The naming follows Spring Kafka's** dead-letter headers (`kafka_dlt-exception-message`, `kafka_dlt-original-topic`, …), so the scheme is familiar to anyone who knows that convention.
- **Order of steps:** `handover_failed` is set (when an order exists), then the dead letter is written, then, **once the broker has confirmed it**, the offset is committed.
- **Dead letters are at-least-once.** A crash after the confirmation but before the commit writes the same dead letter twice. Replaying both is harmless, because processing is idempotent.

**Replay**
- **A small command:** `make replay ARGS="--reason retries-exhausted"`, filtering by reason, order id or time.
- **Dry run by default:** it lists what it would replay; `--execute` does it.
- **It republishes to `orders`:** value, key and original headers, without the `dlt.*` headers and with a `replayed-from` header (`orders.dlt/1/42`). It reads with its own consumer group.
- **Replayed events go through normal processing.** A `handover_failed` order completes, an order already handed over is skipped, and a duplicate dead letter does nothing.

| Reason | Worth replaying after | Default |
|---|---|---|
| `retries-exhausted` | the fulfilment service is back | replayed |
| `rejected` | a mapping bug or the fulfilment side is fixed | replayed |
| `unexpected` | the bug fix is deployed | replayed |
| `invalid` (unsupported type or version) | the consumer supports it | replayed |
| `invalid` (other layers), `conflict` | the producer sends a corrected event | **skipped** unless `--force`: unchanged bytes fail again |

- **Replay `retries-exhausted` dead letters within the fulfilment service's key window** (ADR 0004).

## Considered options

- **Retry mechanism.**
  - *Retry topics* (non-blocking): the main alternative. No head-of-line blocking for per-order failures and long budgets, at the cost of three more topics, consumers that wait until a retry is due, and reordering. In an outage every order fails, so the whole backlog would only move through three more topics. See *Beyond 300 seconds* for when to switch.
  - *Pause and keep polling on every retry:* long backoffs without losing the group, but every retry would have to handle a rebalance taking the partition away. Used only while the circuit is open.
- **Bounds.**
  - *Retrying until success:* not bounded, and a stuck order blocks its partitions.
  - *Shortening a long `Retry-After`:* retries against a service that has said it is unavailable.
  - *Backoff without jitter:* instances retry in step.
- **Flood guard.**
  - *No guard, rely on replay:* simpler and loses nothing, but every outage becomes manual work and the dead-letter topic stops meaning "this message is bad".
  - *Counting invalid events too:* bad producer data would stop valid events.
- **Stopping.**
  - *Exiting at once (and letting Compose restart):* releases partitions, but flaps during a shared outage, re-reading the order in hand on every restart. Kept as the optional exit after N minutes.
- **Unexpected exceptions.**
  - *Crashing:* the same message crashes the restarted consumer again.
  - *Retrying them:* a bug fails the same way every time.
- **Dead-letter topics.**
  - *Two topics, one for the producer's bad data and one for our failures:* mirrors who fixes what, but two topics to watch, and unsupported versions belong to both. The evolution when different teams own them.
  - *One topic per reason:* many topics for little gain.
- **Dead-letter format.**
  - *Wrapping the original in a JSON envelope* (`{ "original": "<base64>", "error": { … } }`): self-describing, but unreadable bytes need base64, the dead letter no longer looks like its event, and replay has to unwrap it.
- **Replay.**
  - *A documented procedure with Kafka's console tools:* no code, but fiddly with headers and filters, and hard for a reviewer to try.
  - *Replaying straight into processing:* a second entry point, with replayed and live events interleaving.
  - *By hand in kafka-ui:* fine for one dead letter, not for fifty.

## Consequences

- **Tests:** one per failure class; the circuit opening, probing and closing; the bug guard; invalid events not tripping the guard; the start-up budget check; a dead letter's headers; replay of a `retries-exhausted` dead letter ending in a handover.
- **Topics:** `orders` (3 partitions, 7 days) and `orders.dlt` (3 partitions, 30 days), with retention set explicitly when the topics are created.
- **`make demo`** shows a fault, a dead letter, the fault cleared, a replay and the handover.
- **Production evolution:** alerting on consumer lag, circuit state and dead-letter growth; exiting after N minutes when instances run on separate hosts; retry topics or an outbox when their triggers appear; separate topics for producer-side and consumer-side failures when different teams own them.
