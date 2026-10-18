---
status: accepted
---

# Rebalances, restarts and shutdown: stop between steps, never commit in a callback, prove it with two assertions

Correctness never depends on a consumer finishing its work: anything not committed is redelivered, and ADR 0004 makes every repeat harmless. So the consumer's lifecycle is tuned for **prompt, predictable hand-overs** instead. On SIGTERM it **finishes the current step, then leaves the group at once**. Rebalances use the **classic protocol with `cooperative-sticky`**, whose failure behaviour has been verified; callbacks **never commit**, and an uncommitted message from a partition that moved is simply dropped. Membership is **dynamic**. Every reliability test ends with the same **two assertions**: nothing lost, nothing duplicated downstream. Terms are defined in [`CONTEXT.md`](../../CONTEXT.md).

## Decision

**Shutdown (SIGTERM, SIGINT)**
- **The signal handler only sets a flag.** The single-threaded loop checks it between steps, so nothing is interrupted halfway through a database transaction.
- **Finish the current step, then stop.** The handover attempt in progress (at most 10 s, ADR 0003) completes; no further retry or message is started.
  - If the message has reached a terminal outcome, its offset is committed.
  - Otherwise it stays uncommitted and is redelivered; the crash table of ADR 0004 makes that safe.
- **`close()` leaves the group at once**, so the instance's partitions move to the other instance immediately instead of after a session timeout.
- **A paused instance stops immediately**; its in-hand message is uncommitted.
- **`stop_grace_period: 20s`** in Compose, leaving headroom over the 10 s attempt.

**Rebalances**
- **The classic group protocol with `partition.assignment.strategy=cooperative-sticky`**: only the partitions that move are revoked; the others keep being processed.
- **When a revocation can arrive.** Callbacks run inside `poll()` and the loop is single-threaded, so only at two moments:

| Moment | In hand | What happens |
|---|---|---|
| Between messages | nothing | nothing; every finished message is already committed, so `on_revoke` commits nothing |
| While paused (ADR 0006) | one uncommitted message, possibly from a revoked partition | if its partition was revoked, it is **dropped** without committing; the new owner processes it from the committed offset |

- **`on_lost`** (membership gone, e.g. after exceeding `max.poll.interval.ms`): anything in hand is dropped and **nothing is committed**. Such a commit would fail anyway (`UNKNOWN_MEMBER_ID`).
- **The circuit state survives a revocation.** It describes the fulfilment service, not the partition; a paused instance keeps probing with the next message on its remaining partitions.

**Membership**
- **Dynamic:** no `group.instance.id`; `session.timeout.ms` stays at its 45 s default.
- **Compose runs one consumer service with `replicas: 2`.**

**Restarts**

| Situation | Exit code | Compose (`restart: unless-stopped`) |
|---|---|---|
| SIGTERM, clean stop | `0` | stays stopped |
| uncaught crash | `1` | restarted; safe by ADR 0004 |
| configuration invalid, or the start-up retry-budget check fails (ADR 0006) | `2` | restarted with Docker's growing delay; the log says why each time |
| still paused after N minutes (optional, ADR 0006) | `3` | restarted; its partitions move in the meantime |

**Health**
- **A heartbeat file** (`/tmp/consumer-health.json`, `HEALTH_FILE`) is rewritten on every loop turn. `poll()` returns at least once a second, even while paused, so the file stays fresh. It holds the state (`starting`, `running`, or `paused` with its reason, e.g. `circuit-open`, `bug-guard`, `systemic: postgres-unavailable`, and the error behind it), the assigned partitions, and **by when the next beat is due**: 30 s after an ordinary turn, and 30 s plus the processing budget (a handover's worst-case retries and a dead letter's confirmation, ADR 0006) before a message is processed, since the loop doesn't poll until it is done.
- **`starting` lasts until the instance holds partitions**, or until it has been given none for 5 s. A member that joins is first given nothing and its partitions a moment later (the cooperative protocol's second round), so only an empty assignment that lasts means the group has more members than partitions.
- **The Compose healthcheck** (`python -m order_consumer.health`, every 5 s, every second during a 120 s start period) reports **unhealthy** when the heartbeat is overdue (the loop is stuck), the state is `paused`, or still `starting`, and prints why. So `make up` (`docker compose up --wait`) returns only once the consumers consume, a paused instance is unhealthy in `docker compose ps`, and `make ps` adds each consumer's last health check, with its reason.
- **In production the two meanings are separated.** Liveness checks only that the heartbeat is fresh. Being paused must not fail liveness, or the orchestrator would turn every pause into a restart loop and defeat the flood guard. Paused is an alert (or a readiness signal) instead.
- **Start-up order:** consumers start once Kafka is healthy, topic creation and migrations have completed, and the fulfilment stub is healthy.

**Proving it.** Every reliability test ends with two assertions:
1. **Nothing lost:** every valid event published ends as an order with `handover_status = handed_over`, and the committed offsets reach the end of `orders`.
2. **Nothing duplicated downstream:** the stub's journal of created fulfilment orders holds exactly one per order id, however many requests arrived, and `orders` holds exactly one row per order id.

| Scenario | How the failure is injected | Additionally shows |
|---|---|---|
| Crash points (ADR 0004) | in-process, against real Kafka, PostgreSQL and the stub: an adapter wrapper simulates a crash right after storing, after the handover, or after recording; then a fresh consumer instance starts | the redelivered event completes, replays or is skipped as the crash table says |
| Hard kill mid-handover | a stub rule slows order X's handover; once the journal shows its request in progress, the consumer is killed and started again | X still has one fulfilment order; its repeat was answered with `409` or the replay |
| Graceful stop mid-stream | SIGTERM while 50 orders are being processed | the other instance takes over at once; the stop takes under 20 s; at most one redelivery |
| Rebalance under load | start with one instance, scale to two halfway | partitions split 2 + 1 |
| Zombie consumer | the test configuration shrinks `max.poll.interval.ms` (and `session.timeout.ms`, which may not exceed it); a stub rule slows one handover past it | the zombie's commit fails and is logged; no second order row or fulfilment order |
| Pause during a rebalance | a stub outage opens the circuit; then the other instance is stopped or started | the dropped in-hand message is processed by its new owner after recovery |

- **Crash points are injected in-process** because a real kill can't reliably land between "the fulfilment service answered" and "we recorded it"; the wrapper makes all three exact and repeatable. The hard-kill test adds one realistic process-level crash on top.

## Considered options

- **Shutdown.**
  - *Finish the whole message, retries included* (`stop_grace_period` ≈ 90 s): no repeats, but a stop could take a minute and a half, and a paused instance would never finish.
  - *Abandon immediately:* fastest, but cuts handovers off mid-request, making `409`s and replays more common than necessary.
- **Rebalance protocol.**
  - *The new consumer protocol (KIP-848, `group.protocol=consumer`):* broker-side assignment and faster rebalances; generally available in the client but not its default. Its behaviour when `max.poll.interval.ms` is exceeded, which the zombie case depends on, hasn't been verified here. The evolution once it has; switching is a configuration change.
  - *Eager assignment* (`range`, `roundrobin`): every rebalance stops all partitions, not just the ones that move.
- **Revocation.**
  - *Committing in `on_revoke`:* nothing is pending to commit, because each message is committed when it finishes.
  - *Keeping an in-hand message from a revoked partition and finishing it:* the new owner would process it too; harmless thanks to idempotency, but pointless work and a confusing log.
- **Membership.**
  - *Static membership* (`group.instance.id` per instance): a quick restart gets its partitions back without a rebalance, which suits large groups with rolling deploys. But a static member doesn't leave the group on `close()`, so a deliberate stop would leave its partitions waiting for the session timeout, and Compose would need one named service per instance. With two instances and three partitions a rebalance costs milliseconds.
- **Health.**
  - *An HTTP health endpoint:* needs a second thread in a deliberately single-threaded consumer; a file says the same.
  - *Paused counted as healthy:* hides the most important state from `docker compose ps`.
  - *A fixed limit, unhealthy when the file is older than 30 s:* simpler, but a handover's retries keep the loop from polling for up to 110 s by design (ADR 0006), so a busy instance would show as stuck, and a liveness probe built on it would restart it mid-retry. The heartbeat says itself how long its next turn may take.
  - *Healthy as soon as the first assignment arrives, even an empty one:* `up --wait` could then return in the moment between the cooperative protocol's two rounds, before the instance holds its partitions.

## Consequences

- **Log events** are needed for partitions assigned, revoked and lost, an in-hand message dropped, the zombie's failed commit, shutdown and each exit with its code.
- **The test harness** must be able to kill, stop and scale consumer containers, shrink timeouts through configuration, and wrap adapters to simulate crashes.
- **Production evolution:** KIP-848 once verified; static membership for large groups with rolling deploys; separate liveness and alerting probes.
