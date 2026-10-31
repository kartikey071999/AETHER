# SCOUT and ROGUE research workers

The factory discovers `ScoutAgent` and `RogueAgent` automatically. Incoming
`research.request` messages create bounded, persisted **ready tasks**; only the
runtime scheduler/worker executes investigations. Both roles can investigate
one objective concurrently. SCOUT compares mainstream approaches; ROGUE uses
divergent generation, feasibility critique and top-k selection, or category-specific
local heuristics when models cannot serve the request.

Configure each constructor with `ResearchLimits`. Defaults permit eight active
objectives, sixteen queued tasks, two self-generated objectives per day, four
sources and two light-tier model completions per objective, two task attempts,
one follow-up delegation and a one-day review deadline. Source/model reservations
are persisted as audited progress messages and survive restart. Model quotas
and tool policy remain authoritative. A source reservation counts one distinct
investigation; approval/retry can reread that source within the task attempt limit.
Interrupted model reservations remain consumed rather than overspending.

Supply trusted `default_sources`, or encode explicit sources in the request's
`scope`:

```json
{"sources":[
  {"kind":"file","location":"README.md","perspective":"conventional"},
  {"kind":"http","location":"https://allowlisted.example/reference","perspective":"unconventional"}
]}
```

`perspective` defaults to `both`; ROGUE prioritizes the reverse source order.
Each source is a bounded investigation with an optional `purpose`. Free-text
scope describes research; it never grants network/device authorization or
generates arbitrary tool commands. With no sources configured, findings remain
untested. Tools are limited to `read_file` and GET `http_request`, using their
existing confined-file and allowlisted-network policies. Approval waits stop
work; a matching, persisted, current WARDEN decision makes a waiting task ready
for the scheduler, without executing it inline.

Questions, excerpts and candidate proposals are quoted `UntrustedContent`.
Instruction-like source content is quarantined, never used for model decisions,
and cannot change permissions. Models cannot assert verified classification or
invent evidence indices. Summaries and rankings are JSON-schema validated;
unavailable, over-budget or invalid responses use deterministic fallbacks. When
no model answers, conventional (SCOUT) research still adopts a
"Standard-library implementation" at confidence 0.65 if all evidence is clean,
at least one source is local (`file:`), and the approach is clearly sufficient:
a capability template is named by the question/scope, or the question/scope or
evidence says the standard library is enough. Otherwise the 0.4 fallback stands.

SCOUT consolidates the **persisted** finding and evidence, not mutable wire
claims: adopt an evidence-linked proposal at confidence >= 0.6, request evidence
when citations are missing/quarantined, delegate uncertain feasibility, or reject
incoherent/low-confidence proposals with rationale. Adoption means evaluation,
not installation or verification. It creates acceptance criteria, requires
security review, submits the proposed requirement to LEDGER, and announces
`research.consolidated` to ORCHESTR, ROGUE and LEDGER. Roadmap candidates remain
`discovered`; only adopted candidates become `proposed`. Discoveries share one
roadmap row per objective and approach name (case-insensitive, with whitespace
trimmed and collapsed). Stable objective-scoped names fit the existing registry
constraint; different objectives retain separate rows. Every finding adds an
idempotent `capability.evidence_added` snapshot linking its persisted evidence and
any adopted requirement. Repeated discoveries merge unreviewed metadata, but
never overwrite proposed or later lifecycle states, including verified entries.
Legacy finding-scoped rows are reused; historical duplicates are not deleted.

State, audit events and outgoing messages commit atomically through existing
repositories and savepoint-bound sessions (SQLite or PostgreSQL). Stable message
IDs and task keys prevent duplicate research work and consolidation on redelivery.
Expired queued work is cancelled at admission; generated objectives due for review
are paused, never falsely completed. Self-generated proposals record rationale,
benefit, cost and stopping conditions; failed verification without a task can
generate one bounded objective.

Assumptions: research callers supply existing objective/task IDs; findings are
already in `ResearchFindingRepository` before submission. Read-only HTTP/file
results stay untrusted; no autonomous independent-verification claim is made.
Requirements currently route through LEDGER because the shared contract disallows
`requirement.submit` to ORCHESTR. ROGUE's local publisher refuses requirements,
implementation and capability registration despite the shared sender table's
over-permissive requirement entry. These shared-contract fixes are delegated to
the coordinator; ROGUE otherwise emits only findings to SCOUT and audited progress.
