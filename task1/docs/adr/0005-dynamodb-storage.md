---
status: accepted
---

# Storage: DynamoDB, one table per purpose, keys designed around the known access patterns

Orders are stored in **Amazon DynamoDB**. Each kind of data gets **its own table**: orders, idempotency records, and rate-limit counters (if rate limiting uses DynamoDB). The orders table is keyed by `id`, with two global secondary indexes (GSIs) for the newest-first lists. The access patterns are few and fixed ([ADR 0004](0004-order-model-and-lifecycle.md) deliberately offers no filters and no arbitrary sorting), so DynamoDB's main weakness, query flexibility, costs us nothing. In return there is no VPC, no connection management, no idle cost, and native TTL. Floci emulates all of it, and CDK deploys it.

## Decision

**Orders table** (`orders`)

| Access pattern | Key / index | Operation |
|---|---|---|
| Get an order | table key `id` (UUID v4) | `GetItem`, **strongly consistent** |
| Change status | table key `id` | `UpdateItem` with condition `status = :expected` (and `version = :v` when `If-Match` is sent), incrementing `version` |
| Delete | table key `id` | `DeleteItem` |
| One owner's orders, newest first | GSI **`byOwner`**: partition `ownerId`, sort `createdAt#id` | `Query`, `ScanIndexForward=false` |
| All orders, newest first | GSI **`byCreated`**: partition `listAll` (constant `"ORDER"`), sort `createdAt#id` | `Query`, `ScanIndexForward=false` |

- **Index projection:** both GSIs use projection **`ALL`**, so a page needs no second read.
- **Sort key and cursor:** the sort key `createdAt#id` gives the stable order cursors rely on (`id` breaks ties between equal timestamps). The cursor is the encoded `LastEvaluatedKey`.
- **Timestamps:** ISO-8601 UTC with **fixed-width milliseconds** (`2026-10-17T09:30:00.123Z`), so they sort correctly as text.
- **Money and items:** stored as `Number` attributes in minor units. `items` is a List of Maps inside the order item, so an order is one item (at most 50 line items, far below the 400 KB item limit).

**Idempotency table.** One item per idempotency key, scoped per owner, expiring through **TTL** (`expiration`, 24 hours). Its exact layout follows whatever implements idempotency (the Powertools default schema if that is chosen).

**Rate-limit table.** Separate from the other two if rate limiting stores counters in DynamoDB. Its layout is decided with rate limiting.

**Settings**

| Setting | Orders | Idempotency | Rate limits |
|---|---|---|---|
| Billing | on-demand | on-demand | on-demand |
| Point-in-time recovery | on (production) | off | off |
| Removal policy | retain + deletion protection in production; destroy locally | destroy | destroy |
| Encryption | AWS-owned key (default) | default | default |
| TTL | none (orders never expire) | `expiration` | per rate-limit design |
| Streams | off | off | off |

## Considered options

- **Aurora Serverless v2 (PostgreSQL).**
  - *For:* any query at any time, filters and sorting for free, joins and full transactions.
  - *Against:*
    - Lambda would have to run inside the VPC, and many concurrent Lambdas exhaust database connections, so it needs RDS Proxy or the Data API.
    - Billing is per capacity unit. Pausing to zero means a slow first request after a pause.
    - Expiring data needs a cleanup job.
    - Floci's RDS support is new and unverified.
  - *Verdict:* rejected; the flexibility it offers is exactly what ADR 0004 chose not to need.
- **Single table for everything (classic single-table design).**
  - *For:* one table to provision, monitor and grant access to.
  - *Against:*
    - Rate-limit counters write on every request: high volume, short-lived, hot keys. They would share backups, throughput limits and alarms with business data.
    - A future audit stream would be flooded with counter updates.
    - Generic keys and mixed item types make the data harder to read.
  - *Verdict:* rejected.
- **Single table for the order domain** (orders + idempotency) **with separate counters.** A sound middle ground. *Rejected* in favour of a table per purpose, the most readable layout, where each table's settings (backup, TTL, retention) fit its data exactly.
- **"All orders" without a constant partition key.**
  - *Scan:* returns items in no order.
  - *Write sharding (`ORDER#<0..N-1>`):* removes the write limit, but listing must query N partitions, merge them, and carry N positions in the cursor.
  - *Monthly time buckets:* only bound partition size. Every new write still lands in the current month, and paging must skip empty months.
- **Provisioned capacity with auto-scaling:** cheaper only at steady, predictable load.
- **Eventually consistent `GetItem`:** cheaper, but a caller could miss their own status change immediately after making it.

## Consequences

- **The "all orders" index has a write ceiling.** Every new order writes to one `byCreated` partition, which sustains roughly 1,000 writes per second. That is orders of magnitude beyond this system's needs, and write sharding is the documented next step if volume ever approaches it.
- **Lists are eventually consistent.** Both lists read GSIs, so an order created a moment ago can be missing from an immediate list for a fraction of a second. `GET /api/v1/orders/{id}` reads the table itself and is always current.
- **No ad-hoc queries.** Reporting or flexible filtering would need an analytics export (e.g. to S3 + Athena), new GSIs per pattern, or a move to a relational store.
- **Indexes must be final before the first deploy on Floci.** Adding a GSI to an existing table requires destroying and redeploying the stack there.
- **Production hardening, documented but not built:**
  - a customer-managed KMS key when compliance demands it
  - DynamoDB Streams for the audit trail (ADR 0004)
  - provisioned capacity once load is steady
