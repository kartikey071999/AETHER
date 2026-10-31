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
  summary. It never uses the network.
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

They cover three paths:

- **Happy path:** all 12 steps.
- **Failing verification.**
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

- ORCHESTR plans FORGE's task before SCOUT adopts an approach. FORGE therefore resolves the
  objective's approved capability requirement at execution time. If FORGE runs before the
  requirement is approved, no verification is requested. The WARDEN review dependency prevents
  this in the planned flow.
- `tasks.started_at` / `completed_at` are not populated by the runtime, so the trace derives
  run windows from `task.transitioned` events.
- LEDGER's test-run store does not fill `test_runs.verdict`. The verdict is read from LEDGER's
  `test.report` to ORCHESTR.
