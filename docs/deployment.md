# Single- and multi-engine deployment

An engine (`aether.runtime.engine.Engine`) polls the message bus, runs agent hooks, schedules
tasks and supervises the worker pool. All coordination lives in the database, so more than one
engine process can share one database. This page says what is safe and where the limits are.

## Supported topologies

| Topology | Database | Status |
| --- | --- | --- |
| One engine process | SQLite or PostgreSQL | Supported. SQLite is for development, tests and the demo. |
| Several engines | PostgreSQL only | Supported; the guarantees below apply. |
| Several engines | SQLite | Not supported. SQLite has no row locks, so claims are only safe inside one process. |

Run `uv run aether db upgrade` before starting any engine after upgrading: migration `0006`
adds the message claim columns and backfills inconclusive test runs. Do not run old and new
engine versions against the same database at the same time.

## What keeps several engines apart

- **Task leases.** A worker acquires a lease on a task with a compare-and-swap before running
  it and renews it every third of `lease_ttl_seconds` (default 60). A task whose lease expired
  is recovered by any engine; the previous owner's late writes are rejected because its lease
  no longer matches.
- **Task state.** Transitions lock the task row (`SELECT ... FOR UPDATE`) and check the allowed
  transition table, so two engines cannot both move a task from `ready` to `scheduled`.
- **Message claims.** `MessageBus.claim` selects pending messages with `FOR UPDATE SKIP LOCKED`
  on PostgreSQL, so engines never claim the same message. A claim records the consumer
  (`claimed_by`), when it was taken (`claimed_at`) and when it expires (`claim_expires_at`).
- **Idempotency.** Agents deduplicate work with idempotency keys and first-write-wins marker
  records, which also makes a redelivered message harmless.

## Message claim lifecycle

1. `claim` marks messages `claimed` for a consumer with an expiry of `message_claim_ttl_seconds`
   (default 60) from now.
2. `mark_processing` renews the expiry. While the engine is alive, a heartbeat loop renews every
   claim it holds every third of the ttl, so a long handler keeps its message.
3. `mark_processed` / `mark_failed` must come from the claiming consumer. If the claim was lost
   (expired and taken over by another engine), the call raises `InvalidMessageTransition` and the
   engine logs a warning instead of overwriting the new owner's result.
4. On a clean shutdown the engine releases the claims it still holds after its worker pool has
   stopped, so another engine picks the messages up immediately (event reason `released`).
5. The recovery loop of every engine requeues messages whose claim has expired (event reason
   `claim_expired`). A crashed engine therefore costs at most one claim ttl of delay, and a
   *live* engine's messages are never requeued behind its back.

Rows written before migration `0006` have no expiry. They count as expired, so the first
engine to start requeues any such in-flight message.

### Delivery guarantee and tuning

Delivery is at-least-once. If a handler outlives its claim (a stalled process whose heartbeat
could not run, or a clock skew larger than the ttl), another engine may run the message again;
handlers are idempotent for that reason. Keep the engines' clocks synchronised (NTP) and the
database the single source of time wherever possible.

`EngineLimits.message_claim_ttl_seconds` (default 60) sets the ttl. Shorten it for faster
failover and lengthen it if the database is often slow to answer heartbeats; the heartbeat
interval is a third of it. It is an engine limit, not an environment setting: pass a customised
`EngineLimits` to `Engine` to change it. Keep it at least as long as the longest expected
database pause.

## Waiting states and timeouts

Long waits are visible and bounded rather than silent (see [e2e.md](e2e.md)):

- `waiting_for_verification`: LEDGER's verification task, waiting for the independent test run.
- `waiting_for_approval`: a task waiting for a WARDEN or user decision.
- ORCHESTR escalates each of these once per wait, plus tasks deferred for resources, after
  `verification_wait_minutes` (30), `approval_wait_minutes` (60) and `resource_deferral_minutes`
  (15) respectively. These are `OrchestrLimits` fields.

With several engines the ORCHESTR tick may run on every engine; the escalation trigger and
marker records are first-write-wins, so a wait is still escalated once.

## Known limits

- Only PostgreSQL gives cross-process safety; the claim and lease SQL is also exercised on
  SQLite for single-process tests.
- The tests marked `postgres` (concurrent claims, expiry, heartbeat, release, requeue) run only
  when `AETHER_TEST_POSTGRES_URL` points at a disposable database.
- `max_concurrent_tasks` is a per-engine limit: N engines can run up to N times as many tasks
  in total. Size it per process. The per-task lease and state locks still keep each task
  single-owner.
