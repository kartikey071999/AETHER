# End-to-end capability workflow

The capability workflow is the spec's Phase 5 scenario (section 22). One runtime runs all seven
agents, the message bus, the scheduler and the security layer. Together they turn a user
objective into a verified capability and a user update. Every step leaves persistent records.
The test suite and the demo read the result back from those records, not from memory.

The capability is deliberately harmless: `text_statistics(text) -> {"words", "lines", "characters"}`,
a pure function with JSON output. The objective carries three completion criteria:

- **AC1** counts `"a b c"` as three words.
- **AC2** reports zeros for the empty string.
- **AC3** counts non-ASCII characters by code point.

## The flow

| # | Step | Persistent evidence |
|---|------|---------------------|
| 1 | ORCHESTR stores the objective and plans dependent tasks: SCOUT/ROGUE research → SCOUT consolidation → WARDEN review → FORGE implementation → LEDGER verification | `objectives`, `tasks`, `task_dependencies` |
| 2 | ORCHESTR sends `research.request` to SCOUT and ROGUE, which research concurrently | `messages`, `task.transitioned` events (overlapping run windows) |
| 3 | ROGUE submits its unconventional finding to SCOUT | `research_findings`, `research.finding` message ROGUE→SCOUT |
| 4 | SCOUT adopts one approach and rejects or asks for evidence on the others, with rationale. The requirement it raises carries the objective's concrete criteria | `research_findings.status/rationale`, `requirements.acceptance_criteria` |
| 5 | LEDGER records the requirement and moves it through its lifecycle until it is satisfied | `requirement.created` / `requirement.status_changed` events |
| 6 | WARDEN decides every FORGE and LEDGER subprocess. Each decision is bound to the operation's input hash | `security_decisions` |
| 7 | The scheduler records why it released each task | `scheduler.decision` events |
| 8 | FORGE writes `text_statistics.py` and `test_text_statistics.py` into the task workspace and smoke-runs the module | workspace files, `execution_runs` |
| 9 | LEDGER independently runs the acceptance tests through its tools | `test_runs`, `test_results` |
| 10 | The capability is marked verified only when every criterion passes | `capabilities.verification_status`, `test.report` verdict |
| 11 | ORCHESTR decides to update the user and completes the objective | `decision.recorded` (`user_update`, `objective_completed`) |
| 12 | MUSE writes the outbox file with separate **Verified** and **Uncertain** sections, records the interaction and confirms delivery | `data_dir/outbox/*.md`, `interactions`, `communication.delivered` |

If verification fails, the objective is blocked instead of completed. In the test this happens
because the module is buggy (AC2 and AC3 fail):

- LEDGER records the failing run and sends a blocker to FORGE.
- FORGE escalates to ORCHESTR.
- No capability reaches the verified stage, and none is registered.

## Harness

`aether.runtime.scenario` holds the reusable pieces:

- `CapabilityScenario(settings, ...)`: builds the real runtime through `build_runtime`, starts the
  engine and submits the objective as MUSE would. `run_until(objective_id, predicate, timeout=...)`
  polls the database. To restart, build a second scenario on the same settings with
  `create_schema=False`.
- `ScriptedCapabilityProvider`: a `MockProvider` that answers each request by its shape. It
  covers research drafts, the FORGE plan (good or `buggy=True` module) and the WARDEN risk
  summary. It never uses the network. By default FORGE builds `text_statistics` from its
  deterministic capability template and never asks a model; pass
  `forge_config=ForgeConfig(llm_refine_templates=True)` to make FORGE try the LLM plan first
  (the failing-verification test does this to get the buggy module).
- `collect_trace` / `ScenarioTrace` / `format_trace`: read everything the run persisted and
  render it step by step.
- `scenario_settings(root)`: self-contained SQLite settings for tests.
- `scenario_security()`: adds the running interpreter to the executable allowlist. Nothing else
  is relaxed, so every subprocess still needs an approval bound to its input hash.
- Short tick and poll intervals are injected through `AgentRuntimeSettings` and
  `build_runtime(engine_limits=...)`. Production defaults are unchanged.

## Running it

Tests (temporary SQLite, mock models; about 60 s for the whole directory):

```powershell
uv run pytest tests/e2e -q
```

They cover these paths:

- **Happy path:** all 12 steps.
- **Failing verification.**
- **LLM runaway (`test_llm_runaway.py`):** three mock models return FORGE plans cut off at
  `max_tokens`, as in the 2026-10-31 incident. With template fallback the objective is still
  verified. Without a template it is escalated. Either way FORGE's LLM calls stay within its
  per-objective budget and the task count stays bounded.
- **Restart:** the engine is stopped after WARDEN's first FORGE approval. A new engine on the
  same database finishes the workflow without duplicate requirements, runs, test runs, verified
  capabilities or outbox files.

Demo against the configured database (normally PostgreSQL; migrate it first):

```powershell
uv run aether db upgrade
uv run aether demo capability            # scripted mock models
uv run aether demo capability --live     # the configured model router (e.g. Groq light tier)
```

The demo prints, from the database:

- the objective and its tasks, with run windows and dependencies
- the findings
- the requirements and their criteria
- WARDEN and scheduler decisions
- ORCHESTR decisions
- the execution and test runs
- LEDGER's verdict
- the capability status
- the outbox path

It exits with status 0 only when the capability is verified and the update was delivered.

`--live` uses whichever providers and budgets are configured (`ProviderSettings` from the
environment). Real models may produce a different plan or a failing module; the trace shows
which. Errors print only the exception type, never credentials. The interpreter allowlist
addition applies to the demo process only.

## Known limitations

- A consolidated objective's FORGE tasks are held until the objective has an approved
  requirement. After `requirement_wait_minutes` (default 10; the scenario harness uses 30 s via
  `ScenarioTiming.requirement_wait_seconds`) ORCHESTR escalates the objective once (kind
  `requirement`); the build stays held until a requirement is approved or a person acts.
- LEDGER's verification task waits for the real test run in its own `waiting_for_verification`
  state (distinct from `waiting_for_approval`). If no run arrives, ORCHESTR escalates once per
  wait after `verification_wait_minutes` (default 30); a task waiting for an approval escalates
  once after `approval_wait_minutes` (default 60). Both are kind `verification_wait` /
  `approval_wait`, never block the objective, and are deduplicated by the escalation window.
- A ready task that the scheduler keeps deferring for resources (CPU, RAM, disk, concurrency)
  escalates once after `resource_deferral_minutes` (default 15), with the deferral reason, as kind
  `resource_deferral`. The demo trace lists it under "ORCHESTR decisions" and marks the pending
  task `[escalated to the user]`. Only deferrals in ORCHESTR's release step are timed; the
  engine's own deferral of a task that is already `ready` is not escalated. The wait start is kept
  per task attempt, so a task released and deferred again within the same attempt reuses its
  original start time.
- An inconclusive verdict is stored as an `inconclusive` test run (`test_runs.status`). Rows
  written before migration `0006` as `cancelled` are backfilled by it.
- A ROGUE LLM budget of one or less leaves no critique call; critique then uses the curated
  alternatives.
- Per-objective LLM budgets and breaker state are durable (seeded from `provider_usage` and
  replayed from `provider.circuit_*` events on first use after a start). Usage recorded before
  migration `0005` has no objective and is not counted. Failure counts below the breaker
  threshold are not persisted. An open breaker half-opens after the cool-down (one probe).
- A task with neither a capability template nor a `forge-spec`, whose single LLM draft fails,
  still blocks. ORCHESTR then bounds and escalates it; there is no generic deterministic plan.

## Workflow guarantees

- **Requirement linking:** when SCOUT's requirement is approved, ORCHESTR sets
  `tasks.requirement_id` on the objective's FORGE and LEDGER tasks and emits
  `task.requirement_linked`. FORGE's "latest approved requirement" guess remains only for
  legacy tasks and logs `forge.legacy_requirement_fallback`. FORGE refuses a linked requirement
  that is not approved (`requirement_not_approved`), and ORCHESTR does not release it before then.
- **LEDGER verification task:** it waits for the FORGE test run (state `waiting_for_verification`)
  and is completed by it.
- **Verdicts and timestamps:** `test_runs.verdict` holds the verdict, and the task repository
  sets `tasks.started_at` / `completed_at`.
- **Escalation:** with `max_replans=0`, ORCHESTR escalates a failed report instead of creating
  a resolution task.
- **Bounded re-planning:** each blocker-resolution task counts as a replan, against
  `max_replans_per_objective` (shared with verification rework) and `max_tasks_per_objective`.
  Every task-creating path checks both limits, including plan materialisation and rework.
  A blocker raised by a resolution task escalates the objective (blocked, plus a MUSE update)
  instead of creating another task. Resolution titles never nest ("Resolve … blocker: Resolve …").
- **FORGE planning:** known capabilities (`text_statistics`) use a deterministic template with no
  LLM call. Otherwise FORGE makes one JSON-mode LLM draft per task (`llm_max_tokens=4096`),
  recorded by a durable marker message. Unusable output is reported to the model governor, and
  with `llm_refine_templates` a failed draft falls back to the template instead of a blocker.
- **LLM spend:** per objective and agent budgets, at most two models per request, one
  same-model retry for length-truncated output, and a consecutive-failure circuit breaker
  (see the README's model-provider section).
- **ROGUE research budget:** the per-objective LLM allowance is split between generation and
  critique by `ResearchLimits.llm_critique_share` (default 0.5), so repeated work cannot use up
  the critique calls.
- **No-model research:** when every model call fails (for example all providers over budget),
  SCOUT still adopts the standard-library approach (confidence 0.65) if the evidence is local
  (`file:` sources), nothing is quarantined, and either a capability template is named by the
  question/scope or the evidence says the standard library is enough. Otherwise the 0.4
  fallback is kept and the requirement wait above bounds the stall.
- **Terminal objectives:** ORCHESTR and the worker agents (SCOUT, ROGUE, FORGE, LEDGER, WARDEN)
  acknowledge work messages for cancelled, completed or failed objectives and record an
  `ignored_terminal_objective` decision: no escalation, replanning or user update.
- **No notification storms:** ORCHESTR sends at most one escalation and user update per
  (objective, kind) per `escalation_window_hours` (default 1); later triggers are recorded as
  `escalation_aggregated`. Tasks waiting for verification, approval or resources escalate once
  per wait episode (task attempt) on top of that window. MUSE suppresses a notice that matches an earlier one by audience,
  objective, notice kind and content hash within `AETHER_MUSE_DEDUPE_WINDOW_MINUTES`
  (default 60), independent of correlation id and task, and emits `communication.suppressed`.
  `tests/e2e/test_budget_exhausted.py` runs the scenario with every provider over budget.
