---
status: accepted
---

# Production evolution: what changes beyond the local stack, grouped by what triggers it

The local stack makes **deliberate simplifications**. This ADR is the single list of them, grouped **by trigger**: what must change before real use, what load would make worthwhile, and what new requirements would bring. Each item points at the ADR whose decision it extends; the detail stays there. Nothing here is built. The design doc summarises it in a short section. No production platform is chosen: the needs are a managed Kafka, a managed PostgreSQL and a container orchestrator.

## Decision

### 1. Before go-live

Ordered roughly by risk: correctness and security first, then operability.

| # | Item | Today | Extends |
|---|---|---|---|
| 1 | **Replicated Kafka:** replication factor 3, `min.insync.replicas=2`, `acks=all` for the dead-letter producer, unclean leader election off | one broker | 0009 |
| 2 | **Kafka security:** TLS, authenticated clients (SASL or mTLS), ACLs per principal: the consumer reads `orders` and writes `orders.dlt`; replay runs as a separate principal | plaintext, no authentication | new |
| 3 | **Fulfilment authentication:** OAuth2 client credentials or mTLS; all secrets from a secret manager and rotated | a static token; fake secrets in Compose | 0003 |
| 4 | **Metrics and alerting:** consumer lag, circuit open, bug guard, dead-letter growth, consumer exits; liveness kept separate from the paused alert | logs and traces only | 0006, 0007, 0008 |
| 5 | **Retention sized to the longest tolerated outage plus catch-up time**, and a **dead-letter runbook**: who triages, and replaying within the fulfilment key window | 7 days for `orders`, 30 for `orders.dlt` | 0004, 0006 |
| 6 | **Close the key-memory gap:** look the order up by `externalReference` before handing over a failed handover again, or agree a key retention with the fulfilment service longer than the dead-letter retention | a documented limit | 0004 |
| 7 | **The real fulfilment contract** replaces ours; only mapping 2 and the HTTP adapter change | our stub | 0003 |
| 8 | **Database operations:** high availability, backups with a rehearsed restore, the `processed_events` purge | one container | 0002 |
| 9 | **Schema governance:** a schema registry or consumer-driven contract tests with the producer, enforcing the compatibility rules | a schema file in this repository | 0001 |
| 10 | **Tests the local stack can't run:** several brokers (leader changes, under-replicated partitions) and load | one broker, functional tests | 0009 |
| 11 | **Erasure of one customer's data** (GDPR). `customer_id` is pseudonymous personal data, and dead letters keep the original bytes, including it, for their retention. Erasure must reach `orders`, `processed_events`, dead letters and logs | not covered | new |
| 12 | **Deployment:** an orchestrator and a pipeline with approvals; the exit-after-N-minutes setting once instances run on separate hosts | Compose | 0006, 0007 |

### 2. Load-triggered

| Change | Signal | Extends |
|---|---|---|
| More partitions and instances | throughput per instance is the limit. Size partitions up front: adding them later moves keys to other partitions | 0005 |
| Batched commits (stored offsets, background commit) | commit latency shows in throughput | 0004 |
| Several orders of one partition in parallel, offsets tracked per key | one partition's throughput is the limit | 0004, 0005 |
| Transactional outbox | a slow or failing fulfilment service holds up consumption | 0004 |
| Retry topics | per-order transient failures become common, or backoffs of minutes are needed | 0006 |
| KIP-848 consumer protocol; static membership | rebalances become expensive in large groups or rolling deploys | 0007 |
| Partitioning `orders`; a connection pool | the table grows large; processing becomes concurrent | 0002, 0005 |
| Trace sampling | trace volume | 0008 |

### 3. Requirement-triggered

| Change | Requirement | Extends |
|---|---|---|
| Lifecycle events as snapshot events with a version guard | orders change after placement | 0001 |
| Separate dead-letter topics for producer data and our failures | different teams own them | 0006 |
| CloudEvents binary mode | routing on metadata without parsing bodies | 0001 |
| Cross-region replication (MirrorMaker 2 or cluster linking) | a recovery objective beyond one region | new |

## Considered options

- **A flat backlog:** says what could be done, not when or why.
- **A production platform sketch** (specific managed services, a cost model): the assignment requires local execution only; naming the needs is enough, and the choice belongs to the platform the organisation already runs.

## Consequences

- **The design doc's production-evolution section** summarises the three groups and names the top before-go-live items: replicated Kafka, Kafka security and erasure.
- **The repository's other CI workflow** has no path filter; worth adding when the work moves to its final repository.
