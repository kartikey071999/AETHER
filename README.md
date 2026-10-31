# AETHER

A local-first Python foundation for the AETHER multi-agent runtime. The current
slice provides validated configuration and message contracts, canonical roles
for all seven agents, PostgreSQL persistence models and migrations, atomic task
state transitions with audit events, and deterministic resource-aware task
selection. It does not yet run autonomous agent workers.

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- PostgreSQL 15 or newer for persistent operation

## Local setup

From PowerShell in the repository root:

```powershell
uv sync --group dev
Copy-Item .env.example .env
```

Set `AETHER_DATABASE_URL` in `.env` to the local PostgreSQL connection string,
and create the target database before running migrations. Configuration can be
validated without opening a database connection:

```powershell
uv run aether check-config
uv run aether agents
```

Apply the schema after PostgreSQL is available:

```powershell
uv run aether db upgrade
```

Run the automated tests:

```powershell
uv run pytest
```

## Current implementation boundary

The seven names and responsibilities are registered centrally; role-specific
reasoning workflows are not implemented. Protected model integrations are available
through the injectable `aether.models` package described below. Objectives,
tasks, requirements, messages, audit events, security decisions, and resource
measurements have relational records. Objective/task repositories currently
cover creation and task transitions, and transitions write audit events in the
same transaction. The scheduler selects from supplied ready-task candidates
using dependencies, deadlines, CPU, RAM, disk reserve, concurrency limits, and
wait-time aging; its decisions can be persisted as events.

Host CPU, available RAM, and free disk are persisted once at runtime startup and
every `AETHER_RESOURCE_SAMPLE_SECONDS` (default 30, minimum 1), independently of
scheduler traffic. `ResourceMeasurementRepository` supports `record`, `latest`,
newest-first bounded `range` queries, and `purge`. Range starts are inclusive and
ends exclusive; timestamps must include a timezone. `resource.measured` audit
events are sampled at `AETHER_RESOURCE_EVENT_SECONDS` (default 300, minimum 60),
with admission serialized in the database across repository instances/restarts.
Retention keeps `AETHER_RESOURCE_RETENTION_DAYS` (default 30), purging at startup
and daily, while preserving all samples linked to nonterminal tasks.

Worker outcomes retain attempt duration in `task.result["resource_usage"]` without
overwriting agent results. Safe subprocess executions retain duration and sampled
process-tree `peak_rss_bytes` in `execution_runs.resource_usage`, including failed,
timed-out, and cancelled runs. RSS is sampled at most every 0.1 seconds; it is not
an exact OS high-water mark, and remains `null` when unavailable or too short-lived
to observe. ORCHESTR contexts expose `resource_measurements` and `execution_runs`;
`ExecutionRunRepository.history(task_id=..., agent=..., operation=..., limit=...)`
provides finished runs for future estimates without changing estimates automatically.

`aether.api.create_app(settings, database=database)` creates the local FastAPI
application with `GET /resources` and the operational routes described below.
Resource history uses `ResourceMeasurementRepository.range` and returns the
standard `items`/`total`/`limit`/`offset` Page envelope, newest first with UTC
timestamps. Optional timezone-aware `since` (inclusive), `until` (exclusive), and
`task_id` filters and `limit` (default 100, 1-1000) / `offset` (nonnegative)
pagination bound responses. A range requires `since < until` when both are
supplied; invalid queries return 422. An injected database remains caller-owned;
the factory opens no listener and does not start a second sampler.

Inter-agent contracts live in `aether.communication.contracts`: one frozen
Pydantic payload model per `MessageType` (`PAYLOAD_MODELS`, `validate_payload`),
plus `ROUTES`/`SENDERS` tables and `check_route` describing which agents may
receive and send each type. `aether.agents.factory.build_agents` builds the seven
agents, importing `aether.agents.<lowercase id>` and its `AGENT_CLASS` when the
package exists and falling back to an inert placeholder otherwise.

`resource.response` is ORCHESTR's reply to a resource request: `request_message_id`,
`requested_by`, `decision` (`granted`, `deferred`, `denied`), `reason`, and optional
`retry_after` in seconds (deferred replies only). The recipient must be the requester.
`objective.rejected` similarly identifies the original request and `generating_agent`,
with a `reason` of `rate_limit`, `active_limit`, `missing_cost`, or `invalid`, plus
an explanatory `detail`. Security blockers may go directly to WARDEN or escalate
through ORCHESTR. `promotion.request` carries FORGE's task, `workspace_path`,
`artifacts`, and `verification_evidence` to ORCHESTR or LEDGER for review;
it never authorizes deployment or executes the workspace.

Objective repositories support replacing editable details (including clearing dates)
and changing the base priority without overwriting the scheduled priority. Task
repositories support audited priority and resource-estimate updates. Requirements
accept caller-chosen UUIDs: identical creation content returns the existing ID without
another event, while conflicting content raises `RequirementConflictError`.
The lifecycle adds `proposed -> evaluating -> approved|rejected|needs_evidence`,
`needs_evidence -> evaluating`, and `approved -> implemented -> verified|failed`.
Existing direct approval and `in_progress`/`satisfied` paths remain supported.
Rejected requirements can only be explicitly reopened by SCOUT with a nonblank reason.

`TestRunRepository` creates running suites, adds individual or batched
`TestResultInput` results, and finishes with a `TestVerdict`. Each write emits one
typed event in the same transaction. Verified verdicts require passing acceptance
criteria and no failing/error results. Inconclusive verdicts use a failed run status
to avoid presenting unverified work as successful; the verdict retains the distinction.
Runs can be queried by capability, task, or execution run, and finished runs are
immutable through the repository. Migration `0004_test_run_verdict` adds the nullable
verdict column, leaving historical verdicts unknown. Authorization request creation
accepts a timezone-aware `now` to set `requested_at` deterministically.

Central permission enforcement lives in `aether.security`: an immutable,
default-deny `SecurityPolicy` (configured once at startup through
`AETHER_SECURITY_*` settings such as JSON lists `AETHER_SECURITY_ALLOWED_EXECUTABLES`
and `AETHER_SECURITY_ALLOWED_HOSTS`), WARDEN-only decisions bound to the SHA-256
hash of the exact operation input, explicit user authorization for high-risk
kinds, and single-use short-lived grants. `aether.tools.ToolRegistry` is the
only path to privileged actions; its adapters (argv-only subprocess with
timeouts, process-tree kill, and scrubbed environment; allowlisted HTTP;
confined file I/O) refuse to act without a matching grant, and external output
is returned as `UntrustedContent`. Every allow and block is an audit event.

A continuously running worker lifecycle, wiring the tool registry into agent
workflows, message delivery/recovery, a complete requirement workflow,
independent test verification, personalization storage, and dashboard
remain future phases. No arbitrary software installation or shell execution is
exposed by this foundation.

## Local API

After applying migrations, run `uv run aether api serve`. The read-mostly FastAPI
service binds to `AETHER_API_HOST=127.0.0.1` and `AETHER_API_PORT=8000` by default;
OpenAPI documentation is at `/docs`. It opens the persistent database without
starting runtime workers or contacting model providers. Run `aether run` separately
to process submitted objectives, authorization responses, and running-task controls.

`GET /health` checks the database schema and reports the latest persisted heartbeat;
it does not assert that workers are alive. Read endpoints are unauthenticated:
`/agents`, `/objectives`, `/objectives/{id}`, `/tasks`, `/tasks/{id}`, `/requirements`,
`/capabilities`, `/security/decisions`, `/security/pending-approvals`,
`/scheduler/decisions`, `/resources`, `/errors`, `/executions`, `/tests`, `/events`,
`/messages`, and `/providers/usage`. Collections return
`{"items": [...], "total": N, "limit": 50, "offset": 0}`.
Use `limit` (1-200, default 50; `/resources` uses 1-1000, default 100) and
`offset` (nonnegative) on every collection, and on task
detail's nested dependencies, executions, and events. Embedded test results,
capability verification runs, and recovery attempts have their own bounded pages
starting at offset zero. `/tasks` filters by `state` and `agent`; `/events` by
`type` and `task_id`; `/messages` by `correlation_id`.
Objective progress is the percentage of completed persisted tasks, not an
independent verification verdict (unknown when there are no tasks).
Provider usage aggregates the current UTC day against configured provider and
model budgets, including outstanding reservations. Empty tables return empty
collections, never demonstration data. Resource measurements are read from
`resource_measurements` through the measurement repository, with optional
timezone-aware `since`/`until` and `task_id` filters; no measurements are invented
when none have been recorded.

Set a private `AETHER_API_TOKEN` to enable administrative endpoints. Missing or
blank tokens return 503 for every write; enabled writes require
`Authorization: Bearer <token>` and compare tokens in constant time. Tokens use
`SecretStr`, never appear in API logs, and must not be passed in URL parameters.
Responses are scrubbed using the shared secret-pattern rules and configured
credential values; validation errors omit raw input, and access logging is
disabled by the CLI. Keep the default loopback binding: reads include operational
and personal message content despite credential redaction. If deliberately
exposing the host beyond loopback, put it behind authenticated HTTPS.

`POST /objectives` accepts the `objective.create` contract (for example,
`{"title": "Evaluate a local capability", "completion_criteria": ["Offline tests pass"]}`),
with `generated=false` only. It publishes a validated MUSE-to-ORCHESTR message
and returns HTTP 202 with `message_id`, `correlation_id`, and `status="queued"`.
It does not claim an objective has already been created.
`POST /authorizations/{id}/respond` takes
`{"decision": "approved"|"denied", "text": "...", "person_id": null, "response_id": null}`.
It calls MUSE's trusted `record_user_response`, returns the linked `request_id`
and `interaction_id` with HTTP 202, and never grants authorization itself.
The request must be pending, unexpired, and already delivered to the supplied
audience (null means the unscoped user outbox); WARDEN still applies its
operation-bound authorization checks. Reuse `response_id` to retry the same response.
`POST /tasks/{id}/pause`, `/cancel`, and `/resume` accept an optional
`{"reason": "..."}`. Pause/cancel apply immediately when possible (HTTP 200),
or queue cooperative control for running workers (HTTP 202), preserving live
leases. Resume requires a paused task or returns 409. Missing records return 404;
unavailable database operations return 503 without exception details.

## Model providers

`aether.models` provides async `GroqProvider`, `AzureOpenAIProvider`,
`AzureAnthropicProvider`, `OllamaProvider`, and a deterministic scripted
`MockProvider`. These are library integrations, not yet startup-wired agent
reasoning workflows. `ProviderSettings` reads `.env` independently of runtime
settings. Credential, endpoint, and API-version fields are `SecretStr`; missing
or empty remote configuration makes that provider unavailable. `.env.example`
lists the exact environment names.

Construct the providers with shared `ProviderSettings` and `PermissionService`,
then create `BudgetGuard(ProviderUsageRepository(database.sessions), settings,
recorder)` and one shared `ModelRouter(providers, guard,
max_concurrent_llm_requests=runtime_settings.max_concurrent_llm_requests)`.
Await `router.complete(CompletionRequest(messages=[{"role": "user",
"content": "..."}], tier=ModelTier.LIGHT, max_tokens=128, agent=AgentId.SCOUT))`.
Task/objective IDs are optional context; responses include token usage, latency,
estimated cost, finish reason, and uncertainty flags. Close the router with
`await router.aclose()` at shutdown. Multiple routers must share a semaphore
if they are to share the global concurrency limit.

Routing prefers suitable installed Ollama models, then Groq. Explicitly
declare local-model suitability with, for example,
`AETHER_PROVIDER_OLLAMA_MODELS={"qwen-local:latest":["light","standard"]}`.
Installed names must match exactly; `OllamaProvider.refresh_models()` refreshes
discovery after installations. Groq's `openai/gpt-oss-20b` serves light/standard;
`openai/gpt-oss-120b` and `qwen/qwen3.8-27b` serve standard/reasoning/premium,
with the cheaper eligible model tried first. Azure `claude-opus-4-8` and
`gpt-6-astra` are premium-only, even with an explicit override. Astra is the
last premium fallback unless `provider="azure_openai"` or `model="gpt-6-astra"`
explicitly selects it. `tier="none"` rejects LLM use for deterministic work.
`response_format="json_object"` or `json_schema={...}` enables structured
output where supported; Anthropic requires a schema for JSON output.
Temperature is optional and omitted by default. Anthropic's installed SDK does
not expose temperature; explicit temperature requests skip that provider with
an error instead of silently discarding the requested sampling control.

Whitespace-only or missing completion text raises `EmptyCompletion(ProviderError)`,
including finish reason, input/output counts, and whether usage is estimated.
The router records the failed attempt's usage and falls back, without retrying
that model. Nonempty text with a length finish reason is returned with that reason.
`ModelInfo.min_max_tokens` defaults to 1; Groq gpt-oss/Qwen and Azure Astra use 128.
Smaller requests are raised to that minimum. Optional `CompletionRequest.max_tokens_cap`
limits output tokens; provider/model per-request budgets limit input plus output.
If either cap cannot accommodate the minimum, admission fails with a clear reason,
without calling the model. Groq gpt-oss requests use `reasoning_effort="low"`.

Every completion attempt reserves a persisted `provider_usage` row before the
call, including retries. A UTC daily quota checks request count, conservative
input/output token estimates, estimated USD cost, per-request tokens, and active
requests. Provider-wide caps cover all models; optional `provider/model` caps
apply additionally. Defaults are:

| Provider | Requests/day | Tokens/day | Estimated USD/day | Tokens/request | Concurrent |
|---|---:|---:|---:|---:|---:|
| Groq | 2,000 | 2,000,000 | 10 | 32,768 | 2 |
| Azure Astra | 50 | 100,000 | 1 | 8,192 | 1 |
| Azure Anthropic | 50 | 200,000 | 5 | 16,384 | 1 |
| Ollama | unlimited | unlimited | no API cost | 32,768 | 2 |

Override caps with `AETHER_PROVIDER_BUDGETS` (JSON); unspecified fields retain
their provider defaults. `AETHER_PROVIDER_MODEL_BUDGETS` adds per-model limits.
PostgreSQL advisory transaction locks serialize admission across processes;
SQLite uses immediate transactions for offline tests. Successful calls replace
reservations with measured usage; failed calls retain conservative estimates
when billable consumption is unknown. Security-blocked calls consume no tokens.
Costs in the ledger round upward to its micro-dollar precision. Final usage,
`provider.usage_recorded`, and `provider.request_completed` commit atomically.
Database failures propagate instead of routing around failed accounting.
Interrupted reservations remain fail-closed until explicit reconciliation:
`await reconcile_stale_reservations(database.sessions,
older_than=timedelta(seconds=settings.timeout_seconds + 30), now=datetime.now(UTC))`.
Choose a safety margin appropriate to the deployment (30 seconds in this example).
Only reservations strictly older than the timeout plus margin become `abandoned`;
counts and costs are retained, their accounting timestamp moves to the recovery
day so even pre-midnight calls count against today's budgets, and each row gets
a `provider.usage_recorded` event with status `abandoned`. This operation is
idempotent, serialized with admission, and returns `ReconciliationSummary`
(`abandoned`, `input_tokens`, `output_tokens`, `cost_usd`). No runtime/CLI recovery
hook is wired yet. Never reconcile while calls older than the chosen margin
could still be in flight.

Groq prices are approximate constants from its model API. Unknown remote model
prices return zero **with `cost_estimate_unknown=True`**, not an assertion of free
inference. Override any model using `AETHER_PROVIDER_PRICES` JSON, for example
`{"groq/openai/gpt-oss-20b":{"input_per_mtok":0.075,"output_per_mtok":0.30}}`.
Both rates must be finite and nonnegative, in USD per million tokens; overrides
apply to routing, reservations, responses, and final accounting. Azure prices
are unknown by default, not guessed. A provider or model USD ceiling refuses
unknown-price models unless the provider's budget explicitly sets `allow_unpriced=true`,
for example `AETHER_PROVIDER_BUDGETS={"azure_openai":{"allow_unpriced":true}}`.
That opt-in cannot enforce actual dollar usage; request/token caps still apply. Byte-based
input reservations are conservative estimates, not model-specific tokenizers.
Timeouts/unknown failure consumption remain estimates, not billing reports.

Runtime startup must merge `provider_hosts(settings)` into
`SecuritySettings.allowed_hosts` before constructing its immutable policy, or
explicitly configure the allowlist. This helper does not mutate security policy.
Providers authorize and redeem `OperationKind.LLM_CALL` grants before SDK calls;
the audited HTTP boundary repeats this check for each actual destination and
binds request/header hashes without retaining prompts or credentials. Redirects,
environment proxies, and implicit SDK retries are disabled. Only 429, 5xx, and
transport timeouts/errors receive bounded retries, each separately accounted.
SDK payload-bearing DEBUG logs are suppressed; events retain safe classifications,
never prompt/response/error bodies. Anthropic's installed SDK uses its existing
`httpx2` transport; Groq, OpenAI, and Ollama use `httpx`.

All default tests are offline with mock SDKs/transports. The optional live smoke
test is skipped unless `AETHER_LIVE_TESTS=1`; it makes at most one Groq completion
attempt with `max_tokens=200`. It uses an isolated test ledger, not the runtime's
daily budget database. Do not run it routinely or treat it as production usage
accounting.

## Observability APIs

`aether.observability.events.build_event(event_type, **fields)` validates a frozen
event envelope and its registered payload. Supply `source`, `actor`,
`aggregate_type`, `aggregate_id`, and `payload`; the category, UUIDs, and UTC
timestamp are generated. Naive timestamps and unknown event types are rejected.
Payload schemas live in `EVENT_TYPES`; provider measurements use `tokens`,
`cost`, and `latency_ms`. Numeric token counts are retained, while credentials,
authorization values, and secret-bearing fields are redacted recursively.

Create `EventRecorder(EventSpool(log_dir / "events.jsonl"))` and use
`recorder.add(session, event)` inside an existing transaction. It neither flushes
nor commits: the caller owns rollback and database failures. Alternatively,
`await recorder.record(database.sessions, event)` commits independently and
returns the existing record for a duplicate event UUID or `idempotency_key`
(first-write-wins). An independent database write failure appends a redacted,
fsynced spool entry and raises `EventWriteError`; a failed spool also raises.
`await spool.replay(database.sessions)` returns `replayed` and `failed` counts,
retains corrupt/failed lines, and preserves appends made during replay.
Spool replay must be serialized across processes by the runtime.

Event metadata is stored in the dedicated `EventRecord` columns (category,
source, actor, correlation/objective/task ids, aggregate, idempotency key) and
`payload` holds only the redacted typed payload; `session_id` and `branch` are
logged but have no columns yet. The mapping is isolated in `_to_record()`, and
`split_legacy_payload()` still reads the older `{"meta", "data"}` shape. All
repositories and the runtime stage events through
`aether.observability.recorder.stage_event` (`build_event` +
`EventRecorder.add`) in the same transaction as the state change; constructing
`EventRecord` outside `aether.observability` is rejected by a test.

`aether run [--once] [--duration SECONDS]` builds the runtime from settings
(spool at `data_dir/spool/events.jsonl`, rotating logs in `log_dir`), applies log
retention once, replays the spool, then starts the engine, which emits
`runtime.started`. The seven agents are inert placeholders until their behaviour
is implemented. `aether events tail [--limit N]` prints the latest redacted
events from the database. `aether demo capability [--live]` runs the end-to-end
capability workflow against the configured database and prints its persisted
trace; see [docs/e2e.md](docs/e2e.md).

`configure_logging(log_dir, max_bytes=..., backup_count=..., level=...,
console=False)` configures delayed, rotating JSON-lines files.
`get_logger(category)` and `log_event(event)` route the nine operational
categories to separate files; objective/task/message events use `runtime.log`,
and capability events use `agent.log`. Every ERROR or higher also reaches
`error.log`, even when the configured category threshold is higher. Messages,
extras, and exception traces are redacted. Reconfiguration closes old owned
handlers without adding duplicates; write failures raise `LoggingWriteError`.
These file logs supplement, rather than replace, the durable database ledger.

`apply_retention(log_dir, archive_after_days=..., delete_archives_after_days=...,
now=...)` archives old numbered rotations into uniquely named gzip files under
`archive/`, returning `archived` and `deleted` counts. Archive age starts at
archiving time. Active logs, spool files, symlinks, and unrelated archives are
excluded. Schedule retention between rotations; it is not a concurrent
multi-process rotation coordinator.

# AETHER — Master System Development Specification

## 1. Mission

Build **AETHER**, a locally running, continuously operating, multi-agent AI system that can pursue persistent objectives, discover new capabilities, research solutions, implement approved changes, verify results, and improve its capabilities over time.

AETHER must run on my Windows machine and use a shared runtime, persistent database, controlled execution environment, resource-aware scheduler, and seven specialized agents.

This is a real software engineering project. Do not build a superficial demonstration consisting of seven chatbots with different system prompts.

The system must support:
- Persistent objectives and autonomous background work.
- Multiple concurrent agents with distinct responsibilities.
- Structured communication between agents.
- Dynamic task creation and delegation.
- Resource-aware scheduling.
- Continuous research and capability discovery.
- Controlled software installation and implementation.
- Security reviews and permission enforcement.
- Automatic operational logging.
- Independent verification of implementations.
- Persistent memory and personalized user communication.
- Recovery after application or machine restarts.
- A modular architecture that supports future expansion.

Inspect the existing repository before making changes. Preserve working code, follow existing conventions where appropriate, and avoid unnecessarily replacing functional components.

Do not ask me to make minor architectural decisions. Use sensible defaults, document important trade-offs, and proceed independently. Ask for clarification only when a decision genuinely requires my input.

Do not claim that functionality is implemented unless the relevant code exists and has been tested.

---

# 2. System Identity and Agent Names

The complete system is called **AETHER**.

It contains seven specialized agents:

| Agent | Primary responsibility |
|---|---|
| ORCHESTR | Central intelligence, coordination, scheduling, and resource management |
| SCOUT | Research, discovery, capability exploration, and roadmap management |
| ROGUE | Unconventional exploration and specialized problem-solving |
| FORGE | Implementation, installation, integration, and maintenance |
| LEDGER | Operational records, requirements tracking, and verification |
| WARDEN | Security evaluation, authorization, and policy enforcement |
| MUSE | Personalization and final user-facing communication |

Use these names consistently throughout the application, source code, agent configurations, dashboard, logs, and documentation.

Use clear, consistent Python module names:

- `orchestr`
- `scout`
- `rogue`
- `forge`
- `ledger`
- `warden`
- `muse`

Do not introduce the previous agent names as aliases or expose them in normal application output.

AETHER is the overall system, not another agent.

---

# 3. Core Architectural Principles

### Local-first operation

The primary runtime, database, scheduler, agent coordination, and operational records must run locally.

External model providers and APIs may be integrated when configured, but the system must not require an external orchestration service to function.

### Persistent operation

Objectives, tasks, requirements, messages, security decisions, execution results, agent state, and important events must survive restarts.

Do not rely on conversation history or in-memory Python objects as the source of truth.

### Continuous autonomy

Agents should be able to pursue ongoing objectives without waiting for a new user message after every operation.

Routine authorized work should proceed independently.

### Distinct agent responsibilities

Each agent must have a clearly defined purpose, permissions, tools, and workflows.

Do not implement seven copies of the same generic LLM wrapper and consider the system complete.

### Resource awareness

Task scheduling must account for available CPU, RAM, disk space, expected execution time, dependencies, and existing workloads.

### Enforced security

Security restrictions must be enforced by the application runtime, not merely stated in an agent's system prompt.

### Observable execution

Important actions and state changes must be automatically recorded and queryable.

### Controlled self-improvement

AETHER may discover, propose, implement, and test improvements, but new capabilities must follow the appropriate approval and verification lifecycle.

### Failure recovery

The system must recover safely from interrupted tasks, process crashes, transient failures, and application restarts.

---

# 4. ORCHESTR — Central Intelligence and Orchestration

ORCHESTR is the central coordinator of AETHER and the primary agent responsible for understanding my objectives.

## Responsibilities

ORCHESTR must:

- Accept objectives from me and other agents.
- Maintain a global view of objectives, tasks, requirements, capabilities, dependencies, and ongoing work.
- Break objectives into tasks and subtasks.
- Delegate work to the appropriate agents.
- Maintain its own long-running objectives.
- Coordinate independent work concurrently.
- Resolve dependencies and execution ordering.
- Assign priorities based on urgency, strategic value, deadlines, dependencies, and resource availability.
- Decide when work should start, pause, resume, be deferred, or be cancelled.
- Identify blockers and route them to the appropriate agent.
- Monitor progress through persistent application state.
- Determine when user input or authorization is necessary.
- Decide when a meaningful user-facing response is ready.

ORCHESTR must not become a bottleneck by requiring manual approval for every harmless internal action.

It should allow independent, authorized work to proceed without unnecessary coordination.

## Resource-aware scheduling

ORCHESTR must consider:

- Available RAM.
- CPU utilization.
- Free disk space.
- Expected download sizes.
- Expected build and temporary storage requirements.
- Estimated task duration.
- Number of running tasks and subprocesses.
- Model availability and expected inference cost.
- Dependencies and critical paths.
- Deadlines.
- Whether tasks can be paused and resumed.
- Current machine health.

For example, if a build may exhaust available disk space, ORCHESTR must defer it, reduce concurrency, or request an appropriate intervention rather than blindly launching it.

Implement a persistent scheduler with explicit limits, configurable policies, and recorded scheduling decisions.

Use actual measurements where possible and estimates when exact resource consumption is unknown.

## Priority management

Priorities must be dynamic.

The scheduler should consider urgency, importance, deadlines, waiting time, dependencies, resource cost, and expected benefit.

Priority decay may reduce the scheduling priority of an objective over time, but must not delete the objective.

New evidence, deadlines, or dependencies can increase its priority again.

---

# 5. SCOUT — Research, Discovery, and Capability Roadmap

SCOUT continuously discovers ways to expand or improve AETHER.

## Responsibilities

SCOUT must:

- Research libraries, frameworks, models, APIs, repositories, algorithms, and tools.
- Identify capabilities that AETHER lacks.
- Investigate implementation alternatives.
- Identify performance bottlenecks and technical weaknesses.
- Maintain multiple concurrent research objectives.
- Accept objectives from ORCHESTR and other agents.
- Generate additional research objectives when justified.
- Break research into manageable investigations.
- Track evidence, feasibility, limitations, dependencies, costs, and risks.
- Maintain an evolving capability roadmap.
- Convert findings into structured implementation requirements.
- Define acceptance criteria.
- Consolidate findings submitted by ROGUE.
- Decide which approaches can proceed through the normal workflow and which require further specialized exploration.

SCOUT owns the consolidated research roadmap and requirements analysis.

It must not automatically install every library or tool it discovers.

## Structured research results

Research findings should include:

- Research objective.
- Question being investigated.
- Findings and supporting evidence.
- Candidate approaches.
- Comparison of alternatives.
- Recommended approach.
- Confidence and uncertainty.
- Dependencies.
- Security considerations.
- Expected resource requirements.
- Implementation requirements.
- Acceptance criteria.
- Recommended next steps.

Distinguish between verified findings, hypotheses, assumptions, and untested proposals.

## Continuous objectives

SCOUT should maintain ongoing research objectives, including objectives that it generates independently.

It must prevent unlimited growth of the objective queue through configurable limits, review dates, resource budgets, and stopping conditions.

---

# 6. ROGUE — Unconventional Exploration and Specialized Problem-Solving

ROGUE investigates approaches that conventional research and implementation workflows might overlook.

ROGUE is not merely a critic or security reviewer.

## Responsibilities

ROGUE must:

- Accept objectives from ORCHESTR.
- Maintain independent exploration objectives.
- Investigate unconventional algorithms, tools, models, repositories, libraries, and architectures.
- Explore alternative local models and specialized technologies where appropriate.
- Investigate less commonly suggested approaches to difficult technical problems.
- Work in parallel with SCOUT.
- Find alternatives when standard approaches fail or have important limitations.
- Produce concrete proposals supported by technical evidence.
- Record feasibility, dependencies, resource requirements, limitations, and risks.
- Submit findings to SCOUT for consolidation.
- Continue specialized investigations when SCOUT identifies a need for further work.

## Relationship with SCOUT

SCOUT owns the consolidated roadmap.

ROGUE explores unconventional options and specialized solutions.

Both may investigate the same objective concurrently.

ROGUE submits its findings to SCOUT, which evaluates their relevance and determines the next steps.

SCOUT may decide to:

- Adopt a finding.
- Reject it with a recorded rationale.
- Convert it into an implementation requirement.
- Ask ROGUE for more evidence.
- Delegate further investigation to ROGUE.
- Handle the implementation through the ordinary workflow.

ROGUE must not bypass the shared requirements system, security controls, scheduler, or execution permissions.

## Example

ORCHESTR identifies a need to determine whether I am present for a voice interaction.

SCOUT investigates conventional libraries, APIs, audio activity detection, and feasible architectures.

ROGUE independently investigates alternative models, specialized tools, repositories, and other technically plausible approaches.

ROGUE submits findings to SCOUT, which consolidates the useful results into a coherent plan.

The implementation then follows the normal security, scheduling, implementation, and verification workflow.

For network or device discovery, operate only within explicitly authorized scope.

---

# 7. FORGE — Implementation, Installation, Integration, and Maintenance

FORGE is the primary implementation worker.

## Responsibilities

FORGE must:

- Install approved dependencies.
- Clone and build approved repositories.
- Configure libraries, tools, and services.
- Implement approved features.
- Build adapters and integrations.
- Configure model providers.
- Execute authorized build and maintenance operations.
- Update existing integrations when approved.
- Diagnose straightforward installation and configuration failures.
- Report implementation progress and blockers.
- Record versions, build results, commands, and relevant artifacts.
- Provide evidence for independent verification.

## Execution requirements

FORGE must retrieve structured requirements and implementation plans from the shared system.

Before executing a task, it must have:

- A task ID.
- A linked objective or requirement.
- Defined acceptance criteria.
- An implementation plan.
- Dependency information.
- Resource estimates.
- Required security clearance.
- An eligible scheduler state.

FORGE must not bypass WARDEN to install or execute unapproved external software.

Use isolated environments where practical.

Prefer pinned dependency versions, reproducible builds, integrity checks, explicit working directories, and logged execution.

Destructive operations require appropriate authorization.

Provide rollback or recovery mechanisms wherever practical.

If an implementation failure requires new research, FORGE should report the blocker and request investigation from SCOUT or ROGUE rather than repeatedly attempting random fixes.

---

# 8. LEDGER — Operational Records, Requirements Tracking, and Verification

LEDGER is the operational record keeper and verification system.

It is not merely a test runner.

## Operational records

LEDGER must track:

- Which agent is working on each objective.
- Current actions and task ownership.
- Active, pending, blocked, paused, failed, completed, and cancelled tasks.
- Objectives created by me or by agents.
- Requirements submitted by agents.
- Requirements awaiting evaluation.
- Security reviews and their outcomes.
- Libraries and tools under investigation.
- Dependencies proposed, installed, updated, or removed.
- Downloads, builds, configurations, and implementation changes.
- WARDEN decisions and relevant evidence.
- FORGE execution progress and results.
- Test suites, results, failures, and regressions.
- Resource utilization and scheduling decisions.
- Blockers, retries, exceptions, and recovery operations.
- Available, incomplete, experimental, disabled, and unavailable capabilities.
- Relevant decisions and relationships between records.

## Shared requirements ledger

All agents must be able to submit structured requirements to a shared, persistent system.

Example workflow:

1. ROGUE discovers a library that might solve a problem.
2. The system records the finding, its source, rationale, potential benefit, and proposed next action.
3. SCOUT evaluates the finding and determines whether it belongs in the roadmap.
4. WARDEN evaluates the relevant security implications.
5. ORCHESTR determines priority and scheduling.
6. FORGE performs the approved implementation.
7. LEDGER records the outcome and verifies it.
8. The capability registry is updated only after the required acceptance criteria pass.

Other agents must be able to query the ledger and discover eligible work.

## Automatic logging

**Critical state changes must be recorded by deterministic runtime code, not by asking an LLM to remember to log them.**

Task creation, task transitions, security decisions, installation attempts, execution results, and verification outcomes must generate durable records automatically.

LLMs may summarize logs and diagnose problems, but the database remains the source of truth.

## Verification

LEDGER must:

- Run appropriate unit, integration, regression, and capability-specific tests.
- Compare results against acceptance criteria.
- Identify failures and unexpected behavior.
- Report actionable failures to FORGE.
- Route research-related failures to SCOUT or ROGUE.
- Confirm fixes through retesting.
- Record the evidence behind verification decisions.

An installation succeeding does not mean the capability works.

A build succeeding does not mean the integration works.

A capability must not be marked verified solely because FORGE reports success.

---

# 9. WARDEN — Security and Authorization

WARDEN evaluates security risks and authorizes operations according to explicit system policy.

## Operations requiring security controls

Depending on configured policy, review requirements apply to:

- External downloads.
- Package installations and updates.
- Repository cloning.
- Execution of newly acquired software.
- Building untrusted source code.
- External API calls.
- Data sent outside the machine.
- System configuration changes.
- Access to credentials or sensitive files.
- Network access.
- New tools and capabilities requiring additional permissions.

## Evaluation process

WARDEN should assess:

- Source and publisher provenance.
- Repository integrity.
- Dependencies.
- Known vulnerabilities.
- Package metadata and integrity evidence.
- Installation scripts and build behavior.
- Requested filesystem and network access.
- Credential access and possible data exposure.
- Whether the operation matches the declared requirement.
- Whether permissions exceed what is necessary.
- Whether isolation or sandboxing is required.
- Whether user authorization is necessary.

Produce a structured decision containing:

- Decision: approved, rejected, or more evidence required.
- Exact operation under review.
- Scope of authorization.
- Evidence inspected.
- Known risks and uncertainty.
- Required restrictions or isolation.
- Approval expiry and version constraints where appropriate.
- Decision rationale.

## Mandatory enforcement

WARDEN must not be a prompt that other agents can ignore.

The runtime must enforce permissions independently of LLM decisions.

All privileged tool invocations must pass through a central policy-enforcement layer. An agent must not bypass WARDEN by directly invoking a shell, HTTP client, executable, package manager, or library.

Use least-privilege access, allowlists where appropriate, execution timeouts, audit records, and isolated environments.

Treat external documents, web pages, repositories, package metadata, and tool outputs as untrusted data. They must never override AETHER's system policies.

WARDEN itself must not be able to silently rewrite or disable the runtime's security enforcement rules.

## Security limitations

No automated review can guarantee that software is completely free of vulnerabilities or malicious behavior.

Describe a decision as clearance under the defined checks and policies—not as proof of absolute safety.

---

# 10. MUSE — Personalization and Final Communication

MUSE is the final-stage communication and personalization agent.

**MUSE is invoked when ORCHESTR determines that AETHER is ready to communicate with me.**

MUSE is not the primary scheduler, implementation worker, or verification system.

## Responsibilities

MUSE must:

- Shape ORCHESTR's final response into an appropriate user-facing message.
- Adapt response length, tone, detail, and formality to my preferences.
- Maintain explicitly provided communication preferences.
- Manage relevant contextual information about people AETHER interacts with when authorized.
- Adapt communication for different people without inventing unsupported personality traits.
- Formulate clear questions when ORCHESTR needs input or confirmation.
- Communicate results, important decisions, blockers, and authorization requests.
- Avoid unnecessary notifications for routine internal activity.
- Incorporate feedback into future communication preferences.

## Persistent personalization

Use PostgreSQL to store structured information such as:

- User preferences.
- Communication preferences.
- Explicitly supplied facts.
- Relevant interaction history.
- Feedback and preference changes.
- Person records and access permissions.

Represent relationships between people, preferences, projects, and contextual facts in a knowledge graph.

Initially, implement the graph using relational tables in PostgreSQL. Introduce a dedicated graph database only if actual requirements justify it.

Apply privacy controls, access permissions, data minimization, and deletion policies.

MUSE should only access information relevant to the current communication task and permitted by the system's access policy.

## Communication handoff

ORCHESTR decides whether a user-facing response is needed and supplies MUSE with a structured communication request containing:

- What happened.
- Relevant results and evidence.
- What has been verified.
- What remains uncertain.
- Important blockers.
- Decisions required from me.
- Whether authorization is required.
- Relevant personalization context.

MUSE then prepares the final message.

My response must be recorded as an event and associated with the relevant objective, task, or authorization request.

Silence must never be interpreted as authorization.

---

# 11. Shared Runtime and Agent Communication

Implement one shared runtime with specialized agents, structured messages, persistent state, a scheduler, and controlled tool execution.

Do not implement isolated chatbots that communicate only through unstructured text.

## Message contracts

Use Pydantic to validate inter-agent messages.

Support fields such as:

- `message_id`
- `source_agent`
- `target_agent`
- `message_type`
- `objective_id`
- `task_id`
- `requirement_id`
- `correlation_id`
- `timestamp`
- `payload`
- `priority`
- `deadline`
- `requires_response`
- `status`

Define structured message types for:

- Objective creation and updates.
- Task creation, assignment, and transitions.
- Research findings.
- Requirement submissions.
- Security evaluations and decisions.
- Resource requests.
- Implementation plans.
- Execution requests and results.
- Test reports.
- Blockers and escalations.
- User authorization requests.
- Capability registration.
- Progress updates.
- Health and resource events.

## Communication rules

- Persist important messages and state transitions.
- Allow agents to query shared objectives, requirements, and capabilities.
- Use targeted messages for specific collaboration.
- Use events for system-wide notifications.
- Associate related work using correlation IDs.
- Prevent duplicate task execution during retries.
- Track message delivery and processing where required.
- Ensure agent failures do not corrupt global state.
- Allow independent tasks to proceed concurrently.
- Use explicit ownership and concurrency controls.
- Keep the database as the source of truth.

An in-memory queue may wake workers, but must not be the only place pending tasks exist.

After a restart, reconstruct pending work from persistent state.

---

# 12. Separate Objectives, Tasks, Requirements, and Capabilities

These are distinct entities and must have separate data models.

## Objective

A desired outcome that may persist over time and contain many tasks.

Example: Improve AETHER's ability to determine whether a user is available for a voice interaction.

## Task

A specific unit of work that advances an objective.

Example: Evaluate two local audio activity detection libraries.

## Requirement

A capability, dependency, implementation condition, security constraint, or acceptance criterion.

Example: The implementation must operate locally and must not transmit microphone audio to an external service.

## Capability

A registered function AETHER can invoke through a defined interface.

Example: Local audio activity detection.

Each entity must have a stable ID and appropriate metadata.

Maintain relationships between objectives, tasks, requirements, research findings, security decisions, implementations, tests, and capabilities.

A completed task does not automatically mean the parent objective is complete.

An objective is complete only when its defined completion criteria have been met.

---

# 13. Resource-Aware Scheduler

Implement a persistent scheduler for background work.

Consider:

- Objective and task priority.
- Priority decay.
- Dependencies.
- Deadlines.
- CPU utilization.
- RAM utilization.
- Free disk space.
- Download and build sizes.
- Estimated execution duration.
- Number of concurrent tasks.
- Model availability.
- Interruptibility.
- Resource budgets.

Provide configurable limits for:

- Concurrent tasks.
- Concurrent builds.
- Concurrent LLM requests.
- Per-task execution time.
- Aggregate resource consumption where measurable.
- Minimum free disk space.
- Retries and retry delays.
- Agent-specific concurrency.

Use measurements when available and estimates otherwise. Record actual resource consumption where practical and improve future estimates.

Prevent agents from spawning unlimited subprocesses or background tasks.

Support states such as:

`pending`, `ready`, `scheduled`, `running`, `waiting_for_approval`, `blocked`, `paused`, `retrying`, `failed`, `completed`, and `cancelled`.

Implement safe recovery for interrupted tasks.

Do not resume a sensitive operation after restart if its authorization has expired or its inputs have materially changed.

---

# 14. Controlled Capability Lifecycle

All new capabilities must follow an explicit lifecycle:

1. **Discovery:** SCOUT or ROGUE identifies a potential capability.
2. **Evidence collection:** Record findings, sources, feasibility, limitations, and benefits.
3. **Requirement creation:** Create a requirement linked to an objective.
4. **Technical evaluation:** Assess fit, dependencies, cost, and resources.
5. **Security review:** WARDEN evaluates relevant operations and permissions.
6. **Prioritization:** ORCHESTR decides when to schedule the work.
7. **Implementation:** FORGE implements the approved capability.
8. **Verification:** LEDGER tests it against acceptance criteria.
9. **Registration:** Register it only when verification requirements are satisfied.
10. **Monitoring:** Track health, versions, and failures.
11. **Maintenance:** Create update, repair, or deprecation requirements when needed.

Not every discovered capability must be implemented.

The capability registry must include:

- Capability ID and name.
- Description.
- Lifecycle status.
- Provider or source.
- Version.
- Entrypoint or adapter.
- Input and output schemas.
- Required permissions.
- Dependencies.
- Resource estimates.
- Verification status.
- Tests and evidence.
- Installation and maintenance metadata.
- Known limitations.
- Owning objective or requirement.

Clearly distinguish between discovered, proposed, approved, installed, tested, verified, disabled, and deprecated capabilities.

Never register an untested capability as fully available.

---

# 15. Persistent Database Architecture

Use PostgreSQL as the primary database for the serious implementation.

Create a normalized schema with migrations.

At minimum, support:

- Agents and agent configuration.
- Objectives.
- Tasks and dependencies.
- Requirements.
- Research findings and evidence.
- Messages and events.
- Security evaluations and approvals.
- Tool and capability registry.
- Installations and versions.
- Execution runs.
- Test suites and results.
- Resource measurements.
- Errors and recovery attempts.
- User preferences and interaction records.
- Knowledge graph entities and relationships.
- Agent health and heartbeat records.

Use JSONB for genuinely flexible metadata, not as a substitute for all structured tables.

Implement transactions, indexes, foreign keys, timestamps, and uniqueness constraints.

Add database initialization and migrations.

Store large artifacts, source repositories, logs, and build outputs on disk, with database references to their locations.

Separate configuration, secrets, database state, logs, downloaded repositories, model files, temporary files, and build artifacts.

Implement retention and cleanup policies that do not delete active work or valuable evidence.

---

# 16. Recommended Technology Stack

Use these technologies unless repository inspection identifies a compelling reason to change them.

- **Python 3.12 or compatible newer version** for the runtime.
- **uv** for Python environment and dependency management.
- **asyncio** for asynchronous workers.
- **Pydantic** for validated contracts.
- **PostgreSQL** for persistent state.
- A supported PostgreSQL driver and migration tool.
- **Ollama** as an initial local-model integration where appropriate.
- A model-provider abstraction supporting additional local and remote providers.
- **FastAPI** for a local API.
- **HTTPX** for HTTP integrations.
- **pytest** and **pytest-asyncio** for testing.
- **Ruff** for linting and formatting.
- Optional **LangGraph** for workflows that benefit from graph-based orchestration.

Do not make every component dependent on a single AI framework.

Do not require an expensive model for deterministic operations.

Model selection must account for task requirements, available hardware, concurrency, and configured resource budgets.

Ensure Windows compatibility for paths, environment variables, subprocesses, process termination, permissions, and startup/shutdown behavior.

---

# 17. Project Structure

Inspect the current repository before adopting or modifying this structure.

A suitable organization is:

```text
AETHER/
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore
├── migrations/
├── config/
├── data/
├── logs/
├── artifacts/
├── workspaces/
├── src/
│   └── aether/
│       ├── main.py
│       ├── config.py
│       ├── runtime/
│       │   ├── engine.py
│       │   ├── lifecycle.py
│       │   ├── scheduler.py
│       │   ├── worker.py
│       │   ├── resource_monitor.py
│       │   ├── recovery.py
│       │   ├── permissions.py
│       │   └── shutdown.py
│       ├── agents/
│       │   ├── base.py
│       │   ├── orchestr/
│       │   ├── scout/
│       │   ├── rogue/
│       │   ├── forge/
│       │   ├── ledger/
│       │   ├── warden/
│       │   └── muse/
│       ├── communication/
│       │   ├── bus.py
│       │   ├── messages.py
│       │   ├── events.py
│       │   └── contracts.py
│       ├── objectives/
│       ├── requirements/
│       ├── tools/
│       ├── security/
│       ├── capabilities/
│       ├── models/
│       ├── storage/
│       │   ├── database.py
│       │   └── repositories/
│       ├── observability/
│       └── api/
└── tests/
    ├── unit/
    ├── integration/
    ├── security/
    ├── recovery/
    └── scheduling/
```

This is a guide, not a requirement to create empty modules.

Keep modules focused, responsibilities explicit, and dependencies manageable.

---

# 18. Base Agent Interface

Implement a shared agent interface supporting:

- Agent identity and configuration.
- Declared capabilities and permissions.
- Accepted message types.
- Objective assignment.
- Task execution.
- Cancellation.
- Structured result reporting.
- Error reporting.
- Health checks and heartbeat.
- Persistent progress updates.
- Access to approved services through dependency injection.
- Resource requests.
- Graceful shutdown.

Each agent must implement role-specific workflows.

Allow different agents to use different models when justified, but do not require a separate model process for every agent.

An agent must be able to work on multiple objectives over time without losing task state.

---

# 19. Continuous Operation and Self-Improvement

The runtime must:

- Start workers and agent services.
- Load persistent objectives and eligible tasks.
- Resume eligible interrupted work.
- Recover stale task leases.
- Process incoming user requests and system events.
- Schedule eligible work.
- Monitor resource availability.
- Record progress and failures.
- Run health checks.
- Shut down gracefully.

Agents may generate objectives independently, but every generated objective must record its rationale, expected benefit, estimated cost, and stopping conditions.

Prevent endless self-generated work using configurable limits for:

- Active objectives.
- Newly generated objectives per interval.
- Subtask depth and task count.
- Per-agent work budgets.
- Retry counts.
- Resource consumption.
- Objective review dates.
- Conditions for stopping or escalating work.

Self-improvement must follow the same requirements, security, scheduling, implementation, and verification process as all other work.

AETHER must not silently rewrite its own permission enforcement or deploy unverified changes to its active runtime.

For changes to the runtime itself, use a separate workspace, automated tests, verification, and an explicit promotion or rollback mechanism.

---

# 20. Local API and Operational Dashboard

Build a practical API and, after the core runtime is stable, a local dashboard.

Expose useful information including:

- Agent status and heartbeat.
- Active objectives and progress.
- Task ownership and state.
- Pending requirements.
- Security decisions and outstanding approvals.
- Scheduled and blocked tasks.
- Resource utilization.
- Recent errors and retries.
- Installation and build history.
- Test results.
- Registered capabilities and verification status.
- Recent decisions and agent communications.

The dashboard must read actual persistent application state.

Do not populate it with hardcoded sample data and claim it is connected to the runtime.

Protect administrative endpoints and sensitive information.

---

# 21. Testing Requirements

Create automated tests covering the following.

## Agent coordination

- ORCHESTR creates and delegates objectives.
- SCOUT and ROGUE investigate concurrently.
- ROGUE findings reach SCOUT.
- SCOUT creates structured requirements.
- FORGE executes eligible tasks.
- LEDGER records and independently verifies outcomes.
- WARDEN reviews and authorizes sensitive operations.
- MUSE is invoked for final communication when requested by ORCHESTR.

## Security

- Unauthorized tool calls are blocked.
- Agents cannot bypass the permission service.
- Rejected operations cannot execute.
- Approvals are limited to reviewed operations and inputs.
- Expired approvals are rejected.
- Untrusted content cannot override system policy.
- Security decisions are durably recorded.

## Scheduling

- Priorities and dependencies affect scheduling.
- Resource limits constrain concurrency.
- Disk-space limits prevent unsafe builds.
- Tasks can be paused and resumed where supported.
- Independent tasks can execute concurrently.
- Retry limits are enforced.

## Persistence and recovery

- Objectives and requirements survive restarts.
- Execution history is persistent.
- Interrupted tasks recover safely.
- Duplicate events do not cause duplicate execution.
- Database failures do not leave inconsistent task states.

## Verification

- Successful installation alone does not mark a capability verified.
- Failed acceptance tests prevent successful registration.
- Regression failures are recorded.
- Fixes are retested and linked to the correct execution record.

Use mocks for external services in the core automated test suite. Routine tests must not require real downloads, credentials, or external APIs.

---

# 22. Implementation Plan

Implement AETHER incrementally in the following order.

## Phase 1: Inspect and plan

- Inspect the existing repository.
- Identify working code and missing components.
- Prepare a concise implementation plan.
- Identify dependencies and architectural risks.
- Preserve existing work unless changes are justified.

## Phase 2: Persistent runtime

Implement:

- Configuration.
- PostgreSQL connection and migrations.
- Persistent objectives, tasks, requirements, and events.
- Agent interfaces.
- Message contracts.
- Event handling.
- Task lifecycle.
- Basic scheduler.
- Structured logging.
- Startup and graceful shutdown.

## Phase 3: Security foundation

Implement:

- Centralized policy enforcement.
- Tool registry.
- Permission declarations.
- Security decision records.
- Scoped authorization.
- Audit logging.
- Safe execution adapters.
- Tests proving unauthorized operations are blocked.

Complete this phase before enabling autonomous installation or execution of arbitrary external software.

## Phase 4: Seven agents

Implement ORCHESTR, SCOUT, ROGUE, FORGE, LEDGER, WARDEN, and MUSE with distinct workflows.

Begin with reliable deterministic behavior and mocked integrations where appropriate. Add real model-backed reasoning through the provider abstraction.

## Phase 5: End-to-end workflow

Demonstrate this complete flow using a harmless local test capability:

1. ORCHESTR creates an objective.
2. SCOUT and ROGUE investigate concurrently.
3. ROGUE submits findings to SCOUT.
4. SCOUT consolidates the findings into requirements.
5. LEDGER records the requirements and workflow state.
6. WARDEN evaluates relevant security operations.
7. ORCHESTR schedules eligible work according to resources.
8. FORGE implements the approved capability.
9. LEDGER runs tests and records the results.
10. The capability registry updates only if acceptance criteria pass.
11. ORCHESTR decides whether a user update is needed.
12. MUSE prepares the final communication.

Do not use arbitrary real-world package installation as the first end-to-end demonstration.

## Phase 6: Recovery and reliability

Test restarts, worker crashes, database failures, retries, cancellation, stale tasks, and concurrent updates.

## Phase 7: API and dashboard

Expose useful operational status, task management, diagnostics, and approval information.

## Phase 8: Documentation and validation

Document installation, configuration, database setup, model setup, startup, shutdown, permissions, tests, and known limitations.

Run the actual tests and report their results.

---

# 23. Coding Standards

- Use type hints.
- Validate external and inter-agent inputs.
- Separate business logic from LLM prompts.
- Separate database access from agent reasoning.
- Use dependency injection for shared services.
- Avoid circular imports and giant modules.
- Avoid unnecessary abstractions.
- Never hardcode credentials.
- Never silently swallow exceptions.
- Use structured error handling.
- Apply timeouts to external calls and long-running operations.
- Use bounded retries with backoff.
- Make operations idempotent where practical.
- Test critical behavior.
- Keep logs useful and free of secrets.
- Record important state transitions automatically.
- Keep configuration outside source code.
- Do not claim mocked behavior is a production integration.
- Do not report tests as passing unless they were actually run and passed.

When a feature is incomplete, mark it explicitly as incomplete.

---

# 24. Definition of Done

The initial AETHER foundation is complete only when:

1. It starts successfully on Windows.
2. PostgreSQL initialization and migrations work.
3. All seven agent definitions load.
4. Agents have distinct roles and validated communication contracts.
5. Objectives, tasks, requirements, messages, and events are persisted.
6. SCOUT and ROGUE can collaborate through the shared workflow.
7. ORCHESTR schedules tasks using dependencies and resource constraints.
8. WARDEN's security enforcement cannot be bypassed by agent-generated tool calls.
9. FORGE can execute a harmless, approved implementation task.
10. LEDGER automatically records important state changes and independently verifies the result.
11. MUSE can prepare user-facing communication from an ORCHESTR request.
12. Interrupted work can be recovered safely.
13. Automated tests cover coordination, permissions, persistence, scheduling, and recovery.
14. A documented startup procedure and configuration example are available.
15. Actual implementation status, test results, incomplete features, and known risks are reported accurately.

Do not claim the complete autonomous system is finished if only the foundation has been implemented.

---

# 25. Final Instruction

Start by inspecting the existing AETHER repository.

Then implement the system incrementally, keeping the architecture aligned with this specification.

Prioritize a working, persistent, secure runtime over superficial demonstrations of agent intelligence.

The goal is not to make seven agents talk to each other in a chat window. The goal is to build a coordinated system in which agents maintain persistent objectives, discover and exchange requirements, investigate different approaches, obtain required approvals, execute scheduled work, verify outcomes, maintain shared operational records, and continue working across restarts.

The responsibilities are fixed:

- **ORCHESTR:** Coordinates the system, prioritizes objectives, and manages resources.
- **SCOUT:** Researches, discovers opportunities, and owns the consolidated capability roadmap.
- **ROGUE:** Explores unconventional approaches and specialized solutions.
- **FORGE:** Implements approved changes and maintains integrations.
- **LEDGER:** Records operations, tracks requirements, and independently verifies outcomes.
- **WARDEN:** Evaluates security and enforces authorization policy.
- **MUSE:** Handles personalization and final user-facing communication.

All seven agents operate within one persistent, observable, resource-aware, security-enforced runtime called **AETHER**.