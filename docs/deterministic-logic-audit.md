# Deterministic logic audit (T-053)

Goal from the user: *the list of tasks an agent can see is produced by Python code, which filters
out finished tasks and tasks not for that agent; an agent that wants finished tasks must ask for
them. Handle more and more in code rather than leaving it to the agents.*

This document records (1) the per-agent work queue that now decides task visibility, (2) an
audit of every place in the seven agents where a model or loose agent logic makes a decision,
(3) what was moved into code, and (4) what deliberately stays with a model and why.

## 1. Per-agent work queues

`src/aether/agents/work_queue.py` (no LLM anywhere):

| Call | Who | Returns |
|------|-----|---------|
| `open_tasks(objective_id=None, states=None, limit=50)` | any agent, own tasks only | non-terminal tasks (pending, ready, scheduled, running, waiting_for_approval, blocked, paused, retrying), ordered by **effective priority** (the scheduler's own formula: priority + capped wait-time aging + deadline urgency, via `runtime.scheduler.effective_priority`), then age, then id. Each item carries `depends_on`, `blocked_by` and `ready_to_run` (ready **and** every dependency completed). |
| `finished_tasks(since=None, limit=50, include_cancelled=False, states=None)` | any agent, own tasks only | explicit opt-in: completed/failed (cancelled only on request), newest first, bounded (`1..1000`). |
| `task_detail(task_id)` | owner, ORCHESTR, LEDGER | one task with description/result/error. Another agent's task raises `TaskAccessDenied` **and records a `task.access_denied` event** (requester, owner, operation, reason; never task content). |
| `task_by_key(key)` | owner, ORCHESTR, LEDGER | idempotency-key lookup with the same ownership rule. |
| `waiting_for_approval(limit)` | WARDEN, ORCHESTR, LEDGER | every agent's tasks waiting for a security decision (oldest first). Others are denied and recorded. |
| `incomplete(task_ids)` | any agent | which of the given dependency ids are not completed (progress only, no content). |

Wiring: `AgentWorkQueue` is a read-only facade bound to one `AgentId` (same pattern as
`AgentTools`/`AgentModels`). The engine injects one per agent as `AgentContext.work`;
`BaseAgent.work_queue(ctx)` returns it (rebound to `ctx.sessions`, so it also works inside SCOUT's
atomic research transaction) or builds one for contexts without an engine (tests, API). A context
whose injected queue belongs to a different agent is refused.

Agents refactored onto it:

* **SCOUT/ROGUE** `_enqueue` and `generate_objective`: idempotency lookups use `task_by_key`; the
  research-queue admission count uses `open_tasks` plus `finished_tasks(states={FAILED})`
  (failed tasks can still be retried, so they keep counting, exactly as before).
* **WARDEN** `resume_waiting_tasks`: `waiting_for_approval()` (now bounded and oldest first; the
  periodic sweep picks up any remainder on its next tick).
* **LEDGER** gating: its parked verification tasks come from `open_tasks(states={waiting_for_approval})`;
  the FORGE tasks a verification waits for come from `task_detail(...).depends_on`.
* **FORGE** preflight: dependency completion comes from `incomplete(...)`.

`tests/security/test_task_query_guard.py` parses every module under `agents/` and fails if a
`select`/`select_from`/`join`/`outerjoin`/`update`/`delete` takes `TaskRecord` (or its columns)
outside `work_queue.py`. The allowlist is explicit and checked for staleness:
`orchestr/store.py` and `orchestr/pressure.py` (ORCHESTR is the coordinator that plans, schedules
and pauses everyone's tasks) and `ledger/queries.py` (LEDGER's system-wide operational read model).
Point lookups of a task the agent was handed (`session.get(TaskRecord, id)`) remain allowed.

## 2. Audit: decisions per agent

Legend: **D** = already deterministic code, **M** = model involved, **→ code** = moved in T-053.

### ORCHESTR
| Decision | Status |
|----------|--------|
| Objective kind (capability / research / implementation) → which plan template | was **M** (the refinement output could set `kind` and re-route the whole plan) **→ code**: always `classify_objective`; a disagreeing model suggestion is ignored and recorded as a `plan_kind_suggestion_ignored` decision. The prompt no longer offers the choice. |
| Base plan (tasks, agents, dependencies, security review before FORGE) | D (`planner.build_plan`, `validate_plan`) |
| Extra tasks proposed by the model | M, but validated by `apply_refinement` (agent allowlist, limits, acyclic, FORGE extras wait for security review); any invalid output falls back to the template |
| Scheduling order, admission, concurrency | D (`runtime.scheduler`, `orchestr.scheduling`) |
| Blocker routing, retries, escalation, replans, completion | D (`orchestr.blockers`, limits, completion criteria) |
| Pause/resume under resource pressure | D (`orchestr.pressure`) |

### SCOUT
| Decision | Status |
|----------|--------|
| Which sources to read, budgets, queue/objective limits, review dates | D (`ResearchLimits`, `bounded_request`, work queue) |
| Classification of a model finding that cites no evidence | was **M** (a draft could label itself `hypothesis` with zero cited evidence) **→ code**: `settle_classification` stores it as `untested` (applies to SCOUT and ROGUE). `verified` was already refused by the schema. |
| Citation validity | D (indices checked against collected evidence; invalid → fallback) |
| Adopt / delegate to ROGUE / needs evidence / reject | D (`decide`: evidence checks and fixed confidence thresholds) |
| Conventional adoption without a model | D (`conventional_fallback`) |
| Summary and candidate comparison text | M — stays (see §4) |

### ROGUE
| Decision | Status |
|----------|--------|
| Budget split between generation and critique | D (`split_llm_budget`) |
| Confidence and classification of the final draft | D (fixed in code; uncited hypotheses now `untested` via `save_finding`) |
| Divergent candidates and critique ranking | M — stays (see §4); invalid rankings fall back to curated alternatives |

### FORGE
| Decision | Status |
|----------|--------|
| Plan source order: explicit spec → deterministic template → model draft | D |
| Preflight (requirement approval, criteria, scope, step limits, tools, dependencies, resources, approvals) | D (`forge.preflight`; dependencies now via the work queue) |
| Model plan draft (only when no spec/template applies) | M — stays; one attempt per task, validated JSON, only registered tools, then full preflight and WARDEN review |

### LEDGER
| Decision | Status |
|----------|--------|
| Test verdicts, verification gating, records, snapshots | D (no model calls at all) |

### WARDEN
| Decision | Status |
|----------|--------|
| Allow / deny / require approval, approval expiry, resuming waiting tasks | D (policy engine, approvals; waiting tasks via the work queue) |
| Risk summary attached to evidence | M — stays; advisory text only, never read by the decision |

### MUSE
| Decision | Status |
|----------|--------|
| Rendering, sections, short/long/detail modes, identifier check, dedupe/suppression, routing of non-handoff notices | D |
| Opening line ("polish") | was **M** (light-tier call whose output was only accepted if it equalled the template) **→ code**: `opening_for(preferences)` only; no model call is made. `MuseSettings.polish` is accepted but has no effect. |

## 3. What moved (with regression tests)

| Move | Code | Tests |
|------|------|-------|
| Agents' task lists come from deterministic work queues | `agents/work_queue.py`, `BaseAgent.work_queue`, engine injection | `tests/unit/agents/test_work_queue.py`, `tests/unit/runtime/test_engine.py::test_engine_injects_a_work_queue_bound_to_each_agent`, `tests/security/test_task_query_guard.py` |
| Scheduler priority formula shared, not re-implemented | `runtime.scheduler.effective_priority` | existing scheduler tests + work-queue ordering test |
| ORCHESTR plan kind | `orchestr/planning.py`, `orchestr/prompts.py` | `tests/agents/orchestr/test_orchestr_intake.py::test_llm_cannot_change_the_deterministic_plan_kind` |
| MUSE opening | `muse/workflow.py`, `muse/agent.py` | `tests/agents/muse/test_delivery.py::test_opening_is_deterministic_and_never_calls_a_model` |
| SCOUT/ROGUE uncited hypothesis → untested | `scout/workflow.py::settle_classification` | `tests/agents/scout/test_workflow.py::test_code_settles_evidence_dependent_classification`, `::test_settle_classification_is_pure_and_only_downgrades_uncited_hypotheses` |

## 4. What stays with a model, and why

* **SCOUT summary/comparison and ROGUE divergent ideas + critique** — genuinely open-ended
  synthesis of untrusted text. Everything that turns the output into a decision (citations,
  classification, confidence thresholds, adopt/delegate/reject) is code.
* **FORGE plan draft** — only when neither an explicit spec nor a deterministic template applies;
  bounded to one attempt, strict schema, registered tools only, then deterministic preflight and
  WARDEN review. Growing the template library is how more of this moves into code.
* **ORCHESTR extra tasks** — optional additions to a deterministic plan, validated and fenced.
* **WARDEN risk summary** — human-readable advisory context; the security decision never reads it.

## 5. Known limitations / follow-ups

* Point lookups (`session.get(TaskRecord, id)`) are still allowed in agents (SCOUT blocker and
  parent-task checks, FORGE, LEDGER, MUSE and WARDEN settling a specific task). They read a task
  the agent was explicitly handed or referenced by a message; routing them through `task_detail`
  would need explicit read rights for those cross-agent relations.
* ORCHESTR and LEDGER keep allowlisted system-wide queries; they are the coordinator and the
  record keeper by design.
* A denial raised inside SCOUT's atomic research transaction is staged in that transaction and is
  rolled back with it (the exception still propagates).
* No API endpoint exposes the per-agent work queue yet.
