---
status: accepted
---

# Order model and lifecycle: line items, forward-only statuses, atomic status changes

An **order** is a small but realistic document: line items in one currency, with a total the API computes itself. It moves through five statuses, forward only, and every status change is an **atomic conditional write**, so concurrent requests can't corrupt it. Retried `POST`s are made safe by an optional `Idempotency-Key`. Lists are paginated by cursor and fixed to newest first; filters are deliberately not offered. The model is just rich enough to give validation, authorization and rate limiting something real to protect, without becoming a commerce system. Terms are defined in [`CONTEXT.md`](../../CONTEXT.md).

## Decision

**Shape**
```json
// POST /api/v1/orders — client sends
{ "currency": "EUR",
  "items": [ { "sku": "ABC-123", "quantity": 2, "unitPrice": 1999 } ] }

// every order response — server adds id, ownerId, status, totalAmount, timestamps, version
{ "id": "3f0c…", "ownerId": "<sub>", "status": "PENDING",
  "currency": "EUR", "items": [ { "sku": "ABC-123", "quantity": 2, "unitPrice": 1999 } ],
  "totalAmount": 3998, "createdAt": "2026-10-17T09:30:00Z", "updatedAt": "2026-10-17T09:30:00Z",
  "version": 1 }
```
- **Money** is an integer in minor units (`1999` = €19.99), never a float. The currency comes from an allow-list (`EUR`, `USD`, `GBP`), so every allowed currency has two decimal places.
- **Indicative limits** (exact values and error codes are fixed in the validation decision): 1–50 items; quantity 1–999; `unitPrice` 0–1,000,000; `sku` matches a simple pattern.
- **`totalAmount` is always computed by the API** and never accepted from a client.
- **`unitPrice` is accepted from the client.** This is a documented simplification, because there is no product catalogue in scope.
- **`id`** is a server-generated **UUID v4**, lowercase. Clients never choose ids.

**Statuses and status changes**
```
PENDING ──► CONFIRMED ──► SHIPPED ──► DELIVERED
   │            │
   └────────────┴──► CANCELLED
```

| Status change | Permission ([ADR 0001](0001-role-based-access-control.md)) |
|---|---|
| `PENDING → CONFIRMED`, `CONFIRMED → SHIPPED`, `SHIPPED → DELIVERED` | `order:status:any` |
| `PENDING → CANCELLED`, `CONFIRMED → CANCELLED` | `order:cancel:own` (owner) or `order:status:any` |

- **New orders start as `PENDING`.**
- **Forward only:** no skipping and no going back. `DELIVERED` and `CANCELLED` are final, and a shipped order can't be cancelled (returns would be a separate future process). ADMIN follows the same rules.
- **A disallowed status change returns `409`.**
- **Requesting the status an order already has returns `200` and changes nothing.** That makes a retried `PATCH` harmless.

**Concurrent changes**
- **Atomic conditional write:** every status change is one write, "set status to X only if it is still Y". The loser of a race (e.g. a manager ships while the owner cancels) gets `409`, because by then its change *is* disallowed.
- **Version on every change:** each change increments `version`.
- **Optional `If-Match`:** `GET` returns `ETag: "<version>"`. `PATCH` accepts an optional `If-Match`, and a mismatch returns `412 Precondition Failed`.

**Deletion**
- **Hard delete in any status** (ADMIN only).
- **Responses:** `204 No Content`, then `404` once the order is gone.

**Retried creates**
- **The header:** `POST` accepts an optional `Idempotency-Key` header (IETF draft *The Idempotency-Key HTTP Header Field*).
- **What is remembered, and for how long:** the key, a fingerprint of the request body and the original response, for 24 hours.
- **Scope:** keys are **scoped per owner** (`sub`), so two users' keys never collide or reveal each other's orders.

| Same key, and … | Result |
|---|---|
| the same body | the original `201` response is replayed; no second order |
| a different body | `422` |
| the first request is still in progress | `409` |

**Listing** (`GET /api/v1/orders`)
- **Visibility:** callers see their own orders unless they have `order:read:any` (ADR 0001).
- **Order:** fixed to **newest first** (`createdAt` descending).
- **Pagination:** cursor-based. `limit` defaults to 10, maximum 100. The response is `{ "items": [...], "nextCursor": "…" | null }`.
- **The cursor** is opaque to clients (a base64url-encoded storage position). A cursor that doesn't decode returns `400`. Because every query is restricted to what the caller may see, a tampered cursor can at most move around inside that set.
- **No filters or sort parameters.** Any query parameter other than `limit` and `cursor` returns `400`.

## Considered options

- **Shape.**
  - *Minimal* (`description` + `amount`): too little to validate.
  - *Rich* (addresses, customer, payment): scope creep with no demonstration value.
  - *Money as floats:* rounding errors.
  - *Money as decimal strings:* valid, but more parsing than integer minor units.
  - *Server-side pricing from a catalogue:* the production answer, out of scope (see Consequences).
- **Statuses.**
  - *Three* (`PENDING`, `COMPLETED`, `CANCELLED`): too little lifecycle to demonstrate rules.
  - *More* (`PROCESSING`, `RETURNED`, `REFUNDED`): more rules for no extra value.
  - *Skipping or going back:* rejected, because it makes the history ambiguous.
  - *`409` for the same status:* rejected, because it would turn harmless retries into errors.
- **Deletion.**
  - *Soft delete:* every read and list must remember to filter deleted items (a classic bug source), and real erasure needs a separate purge.
  - *Soft delete with automatic expiry:* the same, delayed.
  - *Deletion only in final statuses:* rejected; deletion is administrative, not part of the order's lifecycle.
- **Id format.**
  - *UUID v7 / ULID:* time-ordered, but they expose the creation time, and v7 needs Python 3.14 or an extra package. Ordering comes from `createdAt` instead.
  - *Sequential numbers:* guessable, and they need a central counter.
- **Retried creates.**
  - *No protection:* duplicate orders.
  - *Required header:* burdens every client.
  - *Design only:* rejected, because serverless makes retries likely (Lambda timeouts, API Gateway's 29-second limit, SDKs that retry automatically).
- **Concurrency.**
  - *Last write wins:* corrupts the lifecycle (a shipped order ends up cancelled).
  - *Required `If-Match` (`428` when missing):* the stricter option, not worth the client burden while status is the only field that changes.
- **Listing.**
  - *Filters (`status`, `ownerId`):* not required by the assignment, and each one needs its own DynamoDB index.
  - *Arbitrary sorting:* doesn't fit DynamoDB.
  - *Offset pagination:* DynamoDB has no offsets, and an offset gets slower the deeper you page.
  - *No bound at all:* a manager's list would grow with the table and eventually exceed Lambda's 6 MB response limit.
  - *A fixed cap without paging:* bounded, but older orders become unreachable.

## Consequences

- **Prices are trusted from the client.** In production, unit prices must come from a catalogue or pricing service, and a client-supplied price would at most be checked against it. The API already computes the total, so only the price lookup changes.
- **No audit trail inside the table.** Hard deletes make erasure real. In production, an audit or event stream (e.g. DynamoDB Streams archived to S3) records every change, deletes included, outside the operational data.
- **Idempotency needs a small store**, with automatic 24-hour expiry. Storage and implementation are decided separately.
- **Storage must support these access patterns:**
  - get an order by id
  - list one owner's orders newest-first
  - list all orders newest-first
  - conditionally update status and version
  - delete by id
  - return a resumable position for the cursor
- **Hardening left for later:** signing the cursor (HMAC); filters, each with its own index, when a real use case appears.
