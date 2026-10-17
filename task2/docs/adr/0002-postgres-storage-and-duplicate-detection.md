---
status: accepted
---

# Storage and duplicate detection: PostgreSQL, the order id as business key, first version wins

Orders are stored in **PostgreSQL**. The order, its lines and a record of the event that brought it are written in **one transaction**, so they can never disagree after a crash. The **order id is the business key**: it guards against a second business operation for the same order, whichever event delivers it. Event ids are recorded as well, so a **redelivery** can be told apart from a **re-emission**. When an event describes an existing order differently, the **first accepted version wins** and the conflicting event goes to the dead-letter topic for a person to look at. Terms are defined in [`CONTEXT.md`](../../CONTEXT.md).

## Decision

**Two scenarios the design must handle**
- **S1, redelivery.** The consumer stores order `3f1c` and crashes before committing its Kafka offset. After the restart, the same event `7d2e` arrives again.
- **S2, re-emission.** The producer has a retry bug and sends order `3f1c` again as a *new* event `9b04` with the same content.

An idempotency key based on the event id alone catches S1 but not S2: order `3f1c` would be stored and handed on twice. The assignment asks to prevent duplicate *business operations*, and the business operation is "accept this order and hand it on once", so the key is the order id.

**The idempotency key**
- **The order id is the business key.** It is the primary key of `orders` and the value of the downstream `Idempotency-Key` header. It catches S1 and S2.
- **Every event id is recorded too**, with the outcome of processing it. This tells S1 and S2 apart in the logs, and makes "same event id, different content" detectable.

**An event for an order that already exists.** The comparison is made on the normalised Order (the output of mapping 1 in ADR 0001), not on the raw bytes, and not on envelope or processing metadata. Whitespace, key order and fields the tolerant reader ignores are not differences.

| Case | Outcome |
|---|---|
| Same content (redelivery or re-emission) | **Skip and log** (`order.duplicate`, naming redelivery or re-emission). No new effects; the offset is committed. |
| Different content, new event id (a conflicting event) | **First wins.** The conflicting event goes to the dead-letter topic as **permanent** (reason `conflict`, never retried), with the fields that differ. |
| Different content, same event id (a producer bug) | The same as a conflicting event. |

- **"Skip" means no *new* effects, not "do nothing".** If the original order's handover to the downstream never completed, a duplicate must complete it rather than skip it. How unfinished handovers are recognised and completed is decided with the dual-write strategy.

**Schema**
```
orders            order_id PK, customer_id, placed_at, currency, total, item_count,
                  accepted_at, first_event_id, trace_id
order_lines       order_id FK → orders, sku, quantity, unit_price, line_total
                  PK (order_id, sku); CHECK quantity > 0; CHECK unit_price >= 0
processed_events  event_id PK, order_id FK → orders, outcome (accepted | duplicate),
                  received_at, topic, partition, offset, trace_id
```
- **One transaction** writes the order, its lines and the `processed_events` row. A duplicate writes only its `processed_events` row (if its event id is new).
- **The `orders` primary key is the final guard.** When two consumers race on the same order, one insert wins; the other sees the existing order and becomes a duplicate or a conflict.
- **Conflicting events get no `processed_events` row**; the dead-letter topic is their record.
- **Lines are a child table**, so the database enforces the business rules a second time (unique SKU per order, positive quantity) and "which orders contain SKU X?" is one query.
- **The Kafka position** (topic, partition, offset) is stored so that a reviewer can find the exact message for any order in the Kafka UI.
- **Retention:** `processed_events` is not purged. Purging is safe whenever it is needed, because the order id, not the event id, prevents duplicate operations; a purge only loses the redelivery/re-emission distinction for old events. A reasonable rule would be "older than Kafka's retention plus a margin".

**Migrations**
- **Plain, versioned SQL files** in `task2/db/migrations/`, independent of how the application accesses the database.
- **Run by dbmate in a one-shot `migrate` service** before the consumers start; consumers wait until it has completed successfully.
- **The consumer's database user cannot change the schema**: it reads and writes data only.
- **Tests apply the same files** to a fresh database, so the schema under test is the schema that runs.

## Considered options

- **Store.**
  - *PostgreSQL + Redis for the dedup keys:* two stores means two writes that can't be atomic. A crash after the Redis key is set but before the order is stored skips the event forever: it is **lost**. A crash after the order but before the key processes it **twice**. Exactly the failures the assignment asks to prevent.
  - *DynamoDB:* `TransactWriteItems` with conditions could write the order and the marker atomically too, so atomicity is not the deciding point. Rejected because the task needs no AWS (it would bring an emulator back in), a transactional outbox would depend on DynamoDB Streams, and reviewers can't inspect the data with SQL.
  - *SQLite:* a single-writer file can't be shared by several consumer instances in separate containers, which undercuts the consumer group.
  - *MongoDB:* multi-document transactions need a replica set; no advantage here.
- **Idempotency key.**
  - *Event id only:* the common textbook answer; misses S2.
  - *Order id only, without recording event ids:* catches S1 and S2, but can't tell them apart or detect "same event id, different content".
  - *Hash of the payload:* defeated by whitespace and key order, and a legitimate correction would look like a new order.
- **Conflicting events.**
  - *Last wins (overwrite and hand on again):* turns "order placed" into a hidden update, the lifecycle ADR 0001 left out on purpose; the downstream would need an update operation.
  - *First wins, log only:* a business conflict that someone has to resolve disappears into a log line.
- **Schema.**
  - *A `first_event_id` column instead of a `processed_events` table:* records only the first event, so later duplicates leave no trace.
  - *Lines as JSONB:* simpler, but the database can no longer enforce the line rules or answer queries about SKUs.
- **Migrations.**
  - *On consumer start-up:* several instances race to migrate (needs an advisory lock), and the consumer needs schema rights.
  - *Postgres init scripts:* run only on an empty volume, so they can't apply a second change.
  - *Flyway:* well known and plain SQL too, but a JVM image with shifting editions and licensing.
  - *Alembic:* built around SQLAlchemy, which the application doesn't use.
  - *A hand-written runner:* reinvents a solved problem.

## Consequences

- **The downstream receives the order id as `Idempotency-Key`**, so a re-emitted order can't create a second downstream order either.
- **The dual-write strategy must make duplicates complete unfinished handovers**, not just skip them.
- **Conflicting events are permanent dead letters.** The retry strategy must make sure a crash between publishing a dead letter and committing the offset doesn't produce a second, unexplained dead letter, or that a second one is harmless.
- **Two database users:** one for migrations, one with data rights only for the consumer.
- **Production evolution:** a scheduled purge of `processed_events`; partitioning `orders` by time if the table grows large.
