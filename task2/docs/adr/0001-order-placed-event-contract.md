---
status: accepted
---

# Order placed event: one event type, CloudEvents envelope, layered validation, three models

The consumer handles exactly one kind of event, the **order placed event**, which carries a complete new order. Events use the **CloudEvents** envelope in structured mode, so the format is a recognised standard rather than our own. Validation runs in five layers, and an **invalid event is permanent**: it is never retried. The transformation is made explicit as three models joined by two pure mappings, so the producer's contract, our own order model and the downstream's contract can change independently. Terms are defined in [`CONTEXT.md`](../../CONTEXT.md).

## Decision

**Shape**
```json
// Kafka key: the order id.  Kafka header: traceparent (W3C Trace Context)
{
  "specversion": "1.0",
  "id": "7d2e…",                                   // event id (UUID)
  "source": "/orders",
  "type": "order.placed",
  "time": "2026-10-17T09:12:04Z",                  // when the event was emitted
  "dataschema": "urn:task2:schema:order-placed:1", // major version
  "datacontenttype": "application/json",
  "data": {
    "orderId": "3f1c…",                            // UUID
    "customerId": "a9e2…",                         // reference only
    "placedAt": "2026-10-17T09:12:03Z",            // when the customer placed the order
    "currency": "EUR",
    "lines": [ { "sku": "KNE-ESC-0042", "quantity": 2, "unitPrice": 1999 } ],
    "total": 3998
  }
}
```
- **One event type, `order.placed`.** There are no updated or cancelled events. The README states the assumption that the assignment's "order events" are order placements.
- **The event id and the order id are different identities.** A redelivery keeps its event id; a new event about the same order gets a new one.
- **The Kafka key is the order id**, so all events for one order land on one partition.
- **The correlation id travels in a Kafka header**, not in the body, so it can be read even when the body can't. It is the trace id of a W3C Trace Context `traceparent` header (ADR 0008); when the header is missing or invalid, a new trace is started and the event id is logged next to it.
- **Money** is an integer in minor units with an ISO 4217 currency from an allow-list. The allow-list records each currency's exponent, so minor units can be turned into a decimal amount.
- **No personal data.** The customer is a reference id. Dead letters keep the original bytes for the topic's retention, and logs describe events, so personal data in the event would spread to both. Keeping it out is deliberate data minimisation.
- **`placedAt` and `time` are kept apart.** `placedAt` is the business fact; `time` is when the event was emitted. They differ when an event is emitted again later.
- **The currency allow-list:** EUR, USD and GBP (exponent 2), JPY (exponent 0) and KWD (exponent 3), so every exponent in use is exercised. Adding a currency is a one-line change with its exponent.
- **Indicative limits** (fixed in [`order-placed.v1.schema.json`](../../schemas/order-placed.v1.schema.json)): 1–50 lines, quantity 1–999, `unitPrice` 0–1,000,000 minor units, `total` an integer ≥ 0, `sku` upper-case letters and digits in hyphen-separated groups (`^[A-Z0-9]+(-[A-Z0-9]+)*$`, at most 64 characters), `currency` three upper-case letters, the event id, order id and customer id UUIDs, timestamps RFC 3339 with an offset, `placedAt` in the years 2000–2999 (a plausible range that also keeps every valid placement time convertible to UTC).

**Validation**, in order; checking stops at the first layer that fails:

| # | Layer | Examples |
|---|---|---|
| 1 | Unreadable | not UTF-8, not JSON |
| 2 | Not a CloudEvent | missing `id`, `type` or `specversion`; wrong `datacontenttype` |
| 3 | Unsupported type or version | `type` other than `order.placed`; a `dataschema` major version other than 1 |
| 4 | Schema violation in `data` | missing field, wrong type, negative quantity, malformed UUID, timestamp without offset |
| 5 | Business rule | total ≠ sum of quantity × unit price; a SKU twice in one order; currency not allowed; Kafka key ≠ `orderId` |

- **Layers 1–4 are checked with a JSON Schema** (`order-placed.v1.schema.json`), plus the parsing in front of it. Layer 5 is code, because a schema can't express "the total equals the sum".
- **One schema file, one part per layer.** Its `$defs` hold `cloudEvent` (layer 2), `supportedTypeAndVersion` (layer 3) and `orderPlaced` (layer 4), and the file as a whole is all three, so it reads as the complete contract. Where a check sits:
  - *Layer 1:* strict UTF-8 and standard JSON (`NaN` and `Infinity` are not JSON); an empty value, such as a tombstone, is unreadable.
  - *Layer 2:* `specversion` `1.0`, `id`, `source` and `type` present; `id` a UUID; `time`, when present, RFC 3339 with an offset; `datacontenttype`, when present, `application/json` (CloudEvents' JSON format implies it when absent). JSON that is not an object fails here.
  - *Layer 3:* `type` `order.placed` and `dataschema` exactly `urn:task2:schema:order-placed:1`. `dataschema` is optional in CloudEvents but required here, because it carries the major version.
  - *Layer 4:* `data` present and within the limits above.
  - *Layer 5:* total, SKUs, currency and key. **The key is compared as bytes**: the same UUID written in another case would still land on another partition.
- **Patterns follow ECMA-262**, as JSON Schema specifies: `$` is the end of the input. Python's `$` also matches before a final newline, so the consumer matches it as `\Z`; otherwise `"KNE-ESC-0042\n"` would pass.
- **An error** is a JSON Pointer and a message, e.g. `/data/lines/0/quantity` and `0 is less than the minimum of 1`. A missing property is reported at the object that lacks it, as JSON Schema validators do. The dead letter also gives the number of errors, so a cut-off list is visible as one.
- **The Kafka key must equal `orderId`.** A mismatched key puts the order on the wrong partition, which would quietly break the one-partition-per-order assumption.
- **All errors of the failing layer are reported**, each with a JSON Pointer to the field, capped at 20, not just the first one.
- **An invalid event is permanent and never retried.** Retrying the same bytes cannot succeed. It goes to the dead-letter topic with its reason; nothing is stored. How it gets there and what a dead letter carries are decided with the retry strategy.

**Versions and unknown content**
- **The major version lives in `dataschema`**; `type` stays `order.placed` across versions. The consumer supports v1 only.
- **An unknown `type` or major version is an invalid event.** It is either a producer mistake or something not yet supported; in the dead-letter topic it is visible and can be replayed once the consumer supports it.
- **Unknown fields inside v1 are ignored** (tolerant reader): not stored, not forwarded. A major version promises additive changes only, so producers can add optional fields without the consumer having to change in lock-step. Breaking changes need a new major version.

**Transformation**: three models, two pure mappings
```
Event v1 ──(1)──► Order ──(2)──► Downstream request
producer's        our own        downstream's
contract          model          contract
```
1. **Event v1 → Order.** Timestamps are normalised to UTC. Whole numbers written with a fraction (`2.0`, which JSON Schema accepts as an integer) become integers. Line totals and the item count are derived. Unknown fields are dropped. The event id, event time and trace id are kept next to the order as processing metadata, not as order fields.
2. **Order → Downstream request.** The downstream has its own shape, for example money as a decimal string with its currency and the order id as an external reference. The exact contract is decided with the downstream API.

- **Both mappings are pure functions**, unit-tested without Kafka, a database or HTTP. A change in the producer's schema touches only mapping 1; a change in the downstream touches only mapping 2.
- **No silent repairs.** Normalisation changes only how a value is represented (time zone, money format). Business values such as SKUs, currencies or a wrong total are never corrected; they make the event invalid. Otherwise the consumer would be deciding business facts on the producer's behalf.

## Considered options

- **Event types.**
  - *A lifecycle (placed, updated, cancelled):* realistic, but retries and dead letters break the order of events for the same order. If "placed" is being retried or has been dead-lettered, a later "cancelled" arrives first. Handling that properly is a design problem of its own (see *Ordering* below), and the assignment doesn't ask for it.
  - *Snapshot events with a version:* every event carries the full current order and a version number that only increases; the consumer keeps the highest version. This is the chosen path if lifecycle events are ever needed (see Consequences). Built now, the version guard would protect against a case a single event type barely produces.
- **Ordering**, should lifecycle events be added:
  - *Strict per-partition order with blocking retries:* one failing order stalls its whole partition, and dead-lettering the head event still breaks the order for that order's later events.
  - *Version guard (snapshot events):* arrival order stops mattering; needs a producer that sends full snapshots. Preferred.
  - *Per-order parking:* when one event of an order is retried or dead-lettered, later events of that order are parked until it resolves; other orders keep flowing. Correct, but needs a parking store and a release mechanism.
  - *Resequencer:* per-order sequence numbers, holding gaps until filled or timed out. Needs sequence numbers from the producer and timeout handling.
  - *State-machine guard:* apply only valid transitions and retry "not yet applicable" ones; needs a bound, because the earlier event may never come.
  - *Order-independent effects:* e.g. a cancellation wins whenever it arrives. Elegant where it fits, but it doesn't fit every transition.
- **Envelope.**
  - *Our own envelope in the body:* same cost as CloudEvents, but a format we invented and have to explain.
  - *CloudEvents binary mode (metadata in Kafka headers):* lets brokers and tools route on metadata without parsing the body, which suits a high-volume platform. Rejected here because validation would split between headers (code) and body (schema), and a reprocessed or dead-lettered event would depend on every header being copied faithfully.
  - *Correlation id in the body:* unreadable exactly when it is needed most, for an event whose body can't be parsed.
- **Payload.**
  - *Customer name, email and address:* more realistic, but personal data would be copied into dead letters and would have to be kept out of every log line.
  - *Money as decimal strings:* valid, but more parsing than integer minor units. *Floats:* rounding errors.
  - *No stated total (derive it):* simpler, but loses a business rule worth checking. A producer and consumer that disagree about an order's value should be caught.
- **Invalid events.**
  - *Also recording them in a database table:* queryable next to the orders, but rejections would then live in two places that can disagree. The dead-letter topic is the single record.
  - *Log and skip:* loses events and fails the requirement for a dead-letter topic.
  - *Retrying them:* the same bytes fail the same way every time.
- **Unknown content.**
  - *Skipping unknown types* (common on shared topics): hides a producer mistake on a topic that carries one contract.
  - *Best-effort processing of unknown major versions:* processes data we can't validate.
  - *Rejecting unknown fields* (strict reader): turns every harmless producer change into dead letters. The task1 Orders API rejects unknown request fields, and both choices are right for their context: an API can answer "400, fix your request" straight away, while a consumer has no way to talk back to its producer.
  - *Version in `type`* (`order.placed.v1`): visible in every filter, but makes a new version look like a new kind of event.
- **Transformation.**
  - *Pass-through* (store and forward `data` as it is): leaves nothing to transform and couples the downstream to the producer's contract.
  - *Enrichment* (looking up customer or product data): adds another external dependency with its own failure modes, out of proportion here.
  - *Normalising business values* (upper-casing SKUs, fixing totals): silently changes facts the consumer doesn't own.

## Consequences

- **Duplicates are the only reprocessing hazard.** With one event type there is no ordering between events to protect, so the retry strategy is free to retry outside the partition's order. Adding lifecycle events would change that: the retry design would then need the version guard or per-order parking.
- **Two different events for the same order** (a new event id with the same or different content) are possible and are handled by duplicate detection, decided with storage.
- **Dead letters carry the original bytes** and a reason that names the layer and the failing fields.
- **Evolution path:** lifecycle events as snapshot events with a version guard; a schema registry for managing schema versions across teams; CloudEvents binary mode if routing on metadata becomes useful.
