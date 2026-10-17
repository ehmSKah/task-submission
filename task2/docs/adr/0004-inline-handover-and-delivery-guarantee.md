---
status: accepted
---

# Storing and handing over: inline, with the uncommitted offset as the to-do list

Storing an order in PostgreSQL and handing it over to the fulfilment service are two writes to two systems, and no transaction spans both. The consumer does them **inline**: store, hand over, record the handover, then commit the Kafka offset. Until the offset is committed, Kafka keeps the event, so **the uncommitted offset is the durable to-do list**: a crash at any point means the event comes back, and the earlier decisions make every repeat harmless. The result is **effectively-once business operations**: each valid order is stored once and handed over at most once. A **transactional outbox** was the main alternative; it is the named evolution for when fulfilment outages or throughput make it worth its cost. Terms are defined in [`CONTEXT.md`](../../CONTEXT.md).

## Decision

**The steps for one event**
1. Validate and transform (ADR 0001).
2. **Store**, in one transaction: order, lines and processed-event row, with `handover_status = awaiting_handover` (ADR 0002). For a duplicate, only check what is unfinished.
3. **Hand over**: `POST /fulfilment-orders` with the order id as `Idempotency-Key`, retried as the retry strategy decides (ADR 0003).
4. **Record**: `handover_status = handed_over`, `fulfilment_order_id`, `handed_over_at`.
5. **Commit the offset.**

**Why every crash is safe.** The offset is not committed until step 5, so a crash anywhere means the event is redelivered:

| Crash after … | What the redelivered event finds | Why it's safe |
|---|---|---|
| step 2 (stored, not handed over) | the order exists, handover unfinished | a duplicate **completes** an unfinished handover (ADR 0002): nothing is lost |
| step 3 (handed over, not recorded) | the order exists, handover unfinished | the repeated handover gets the **`201` replay** with the same fulfilment order id (ADR 0003): no second fulfilment order |
| step 4 (recorded, not committed) | the order is handed over | skipped and logged: no new effects |

Each row is an integration test.

**Committing offsets**
- **An offset is committed only when its message has reached a terminal outcome:**

| Outcome | Commit |
|---|---|
| handed over | yes |
| duplicate, nothing unfinished | yes |
| dead-lettered (invalid, conflicting, rejected, retries exhausted) | yes, once the broker has confirmed the dead letter |
| systemic failure | **no**: consumption stops and the backlog waits in Kafka |
| crash or kill | no: redelivered after the restart |

- **A synchronous commit per message** (`enable.auto.commit=false`, `commit(message=…, asynchronous=False)`). At most one message is redelivered after a crash, which keeps the restart tests exact. The cost, one broker round trip per message, doesn't matter at this scale.
- **One message at a time.** A single-threaded poll loop handles messages one after another, so offsets are committed in sequence without gaps.
- **`auto.offset.reset=earliest`**, so a new consumer group reads existing events instead of skipping them (the default is `latest`). The client's default offset handling (`enable.auto.offset.store=true`) could commit before processing finishes, so it is never relied on.

**Handover state** on `orders` (adds to the schema in ADR 0002)

| Column | Meaning |
|---|---|
| `handover_status` | `awaiting_handover`, `handed_over` or `handover_failed` |
| `fulfilment_order_id` | the fulfilment service's id; set when handed over |
| `handed_over_at` | when the handover was confirmed |

- **An unfinished handover** is any status other than `handed_over`. A duplicate or a replayed dead letter completes it.
- **`handover_failed`** means the consumer gave up: retries ran out, or the fulfilment service refused the order (the dead letter's reason tells which). It is set **before** the dead letter is written; a crash in between only causes a redelivery and another attempt. It is not final.
- **Error details are not copied into the table.** The dead letter and the logs are their single record. Operators find failed handovers with `WHERE handover_status = 'handover_failed'`.

**The delivery guarantee**

> Every valid order placed event in the topic leads to exactly one stored order and at most one fulfilment order. The fulfilment order is created once the fulfilment service is available and any dead letters have been replayed.

Consumption is **at-least-once** (offsets follow terminal outcomes). Effects are **idempotent** (the order id is the `orders` key and the `Idempotency-Key`). Together that gives **effectively-once business operations**. This is not Kafka's exactly-once, which covers only Kafka.

**Where the guarantee stops**
1. **The fulfilment service's memory of keys.** A handover may time out after the fulfilment service has created the order; if retries then run out and the dead letter is replayed after the service has forgotten the key (real services typically keep keys for about a day), a second fulfilment order can be created. Replays should happen within the key window; our stub never forgets. The production fix is to look the order up by `externalReference` before handing over a `handover_failed` order again.
2. **It starts at the topic.** Events the producer never wrote are outside it.
3. **Kafka retention.** A consumer that is down longer than the topic's retention loses the expired events.
4. **Dead letters are at-least-once.** A crash after the broker confirmed a dead letter but before the commit writes it twice. Dead letters are not business operations, so replaying both copies is harmless.
5. **Systemic failures delay; they don't lose.** The backlog waits in Kafka until the configuration is fixed.

**Ordering**
- **Kept:** all events of one order share a partition (Kafka key = order id), and a partition is processed one message at a time, in order.
- **Not kept:** any order across partitions; a replayed dead letter is processed long after its neighbours.
- **Nothing depends on order.** With one event type (ADR 0001), the only repeat for an order is a duplicate, which is harmless whenever it arrives. The retry strategy may therefore reorder (retry topics are allowed).
- **A zombie consumer is covered.** A consumer that blocks past `max.poll.interval.ms` loses its partition but may still finish its message while the new owner processes the same event. Only one `orders` insert can win (ADR 0002), the second handover gets `409` in progress or the `201` replay (ADR 0003), and the zombie's commit fails because it is no longer a group member.

## Considered options

- **Transactional outbox:** store the order and an outbox row in one transaction, commit the offset straight away, and let a relay hand over (`FOR UPDATE SKIP LOCKED`). The textbook answer to dual writes, and it decouples consumption from the fulfilment service: an outage only grows the outbox while Kafka keeps flowing. Not chosen because:
  - It needs a second loop with its own retries, backoff and "give up" state.
  - Handovers that run out of retries would end as a database status, not in the dead-letter topic, so "messages that cannot be processed" would be split across two places.
  - Retries and dead-lettering, which the assignment asks to see, would move away from Kafka.
  - Inline is already correct here, because the downstream is idempotent and duplicates complete unfinished handovers.

  **When to switch:** fulfilment outages long or frequent enough that blocking consumption hurts, or throughput where a slow downstream shouldn't hold up the topic.
- **Hand over first, then store:** idempotency would even make the crashes safe, but "first wins" for conflicting events (ADR 0002) needs the stored order before the handover; otherwise a conflicting event would reach the fulfilment service first.
- **Store with a pending status and a sweeper:** the outbox with the orders table as the outbox; the same trade-offs.
- **Kafka transactions** (`send_offsets_to_transaction`): make Kafka writes and the offset commit atomic, but cover neither PostgreSQL nor HTTP.
- **Committing.**
  - *Store offsets after each message and commit in the background* (`enable.auto.offset.store=false`, `store_offsets`, auto-commit every 5 s, synchronous commit on revoke and close): better throughput; after a crash up to 5 s of messages are redelivered. Idempotency covers that, so switching changes performance, not correctness.
  - *Committing before processing:* loses the event if the consumer crashes.
- **Handover state.**
  - *Two nullable columns only:* "still in progress" and "given up" would look the same.
  - *A table of every attempt:* a full audit, but a write per attempt that repeats what the logs already hold.
- **Closing the key-memory gap now** (a lookup endpoint in the contract and the stub): it needs a timeout, exhausted retries and a replay after more than a day, all together; naming it is enough for this scope.

## Consequences

- **A fulfilment outage holds up consumption**, because retries happen while the message is in hand. The retry strategy must keep them within the poll budget (or pause the partition), and should keep a long outage from turning every event into a dead letter.
- **Tests:** one per row of the crash table, a zombie-consumer test, and a restart test that expects exactly one redelivered message.
- **Production evolution:** a transactional outbox when the triggers above appear; lookup by `externalReference` before re-handing a failed handover; processing several orders of one partition in parallel, with offsets tracked per key.
