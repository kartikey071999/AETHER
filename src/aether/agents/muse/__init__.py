"""Personalization and final communication from structured ORCHESTR handoffs.

The agent factory discovers ``AGENT_CLASS`` without shared runtime changes. Supply
``MuseSettings(data_dir=...)`` to the constructor or as ``ctx.services["muse_settings"]``
when the runtime data directory differs from the default ``data`` directory.

Only communication.request produces user-facing messages. Other contract-accepted notices
are recorded, not promoted to communication requests. Outbound artifacts always use the
bound MUSE tool facade; denied writes remain staged, never delivered. The facade's only write
is the narrow ``deliver_outbox_message`` tool (``communication.deliver``): one new
``<UTC timestamp>-<uuid>.md`` file directly in ``data_dir/outbox``, never overwritten.
``resume_pending(ctx)``
replays incomplete deliveries and responses after restart. ``execute_task`` resumes existing
handoffs for that task; it never treats a task description as permission to communicate.

Supported explicit preferences are length (short/normal/long), tone (neutral/direct/warm),
detail (minimal/normal/detailed), formality (neutral/casual/formal), and channel (file/console).
Both channel hints have a durable markdown artifact; a CLI can consume the console hint.
All factual content is preserved even in short mode. The fact-free opening line is chosen
deterministically from explicit tone/formality; no model is called while composing a message.

Optional handoff personalization_context keys:
decision_options and decision_deadlines map each exact decision string to its choices and
timezone-aware deadline; authorization_expires_at is timezone-aware; routine explicitly
marks suppressible activity; context_entity_ids and context_names jointly allowlist relevant
owned KG names. KG access additionally requires preferences.read, an active full-access
audience, and a matching objective_id/task_id attribute. No attributes enter the message.
Missing options/deadlines are disclosed as not supplied, never fabricated.

record_user_response, record_feedback, and delete_person_data are trusted CLI/API services:
the caller must authenticate user identity and privacy authority before invoking them.
User replies never imply approval from their wording. Only an explicit UserDecision is
forwarded to WARDEN, which remains the authorization authority. Reuse response_id/feedback_id
for retries. Silence is rejected. Supported feedback commands include "shorter please",
"longer please", "more/less detail please", "more/less formal please", "warmer please",
and "more direct please".

Local repository extensions use existing registered decision.recorded events for durable
idempotent checkpoints; outbound/inbound interactions each have interaction.recorded events.
Deletion atomically soft-deletes and anonymizes the person and purges owned preferences, KG
entities and touching relations. Existing interaction/outbox/audit history is retained; MUSE
does not read or resume it for deleted people. Retention of historical artifacts and audit
events is a separate coordinator policy, not an implicit authorization to purge the audit.
"""

from aether.agents.muse.agent import (
    MuseAgent,
    MuseSettings,
    delete_person_data,
    record_feedback,
    record_user_response,
)

AGENT_CLASS = MuseAgent

__all__ = [
    "AGENT_CLASS",
    "MuseAgent",
    "MuseSettings",
    "delete_person_data",
    "record_feedback",
    "record_user_response",
]
