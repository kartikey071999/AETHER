"""Independent unconventional generation, critique and top-k selection; SCOUT owns adoption."""

import json

from aether.agents.base import AgentContext
from aether.agents.rogue import prompts
from aether.agents.rogue.schemas import CritiqueDraft, DivergentDraft
from aether.agents.rogue.workflow import alternatives
from aether.agents.scout.agent import ScoutAgent
from aether.agents.scout.schemas import FindingDraft, ResearchLimits, ResearchWork
from aether.communication.contracts import (
    ResearchConsolidatedPayload,
    receivable_types,
    validate_message,
)
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.security.untrusted import UntrustedContent
from aether.storage.models import FindingClassification


class RogueAgent(ScoutAgent):
    agent_id = AgentId.ROGUE
    accepted_message_types = (
        ScoutAgent.accepted_message_types - {MessageType.RESEARCH_FINDING}
        | {MessageType.RESEARCH_CONSOLIDATED}
    ) & receivable_types(AgentId.ROGUE)

    def __init__(self, limits: ResearchLimits | None = None) -> None:
        super().__init__(limits)

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        if message.message_type is MessageType.RESEARCH_CONSOLIDATED:
            self.ensure_accepts(message)
            if message.target_agent is not self.agent_id:
                raise ValueError("message is addressed to a different agent")
            payload = validate_message(message)
            if not isinstance(payload, ResearchConsolidatedPayload):
                raise ValueError("invalid consolidation notification")
            # Follow-up work arrives separately as a bounded research.request.
            ctx.logger.info("research.consolidation_received", extra={"agent": self.agent_id.value})
            return
        await super().handle_message(message, ctx)

    async def summarize(
        self, work: ResearchWork, contents: list[UntrustedContent], ctx: AgentContext
    ) -> FindingDraft:
        fallback = alternatives(work.request.question, contents, self.limits.top_k)
        if any(content.suspicious_patterns() for content in contents):
            ctx.logger.warning("research.source_quarantined", extra={"agent": self.agent_id.value})
            return fallback
        divergent = await self.complete_json(
            DivergentDraft, prompts.DIVERGE, self.prompt_data(work, contents), work, ctx
        )
        if divergent is None:
            return fallback
        critique = await self.complete_json(
            CritiqueDraft,
            prompts.CRITIQUE,
            UntrustedContent(
                source="candidate-proposals",
                content=json.dumps([item.model_dump(mode="json") for item in divergent.candidates]),
            ).quote(16384),
            work,
            ctx,
        )
        if critique is None or any(
            index < 0 or index >= len(divergent.candidates) for index in critique.ranked_indices
        ):
            ctx.logger.warning("research.critique_fallback", extra={"agent": self.agent_id.value})
            return fallback
        candidates = [
            divergent.candidates[index] for index in critique.ranked_indices[: self.limits.top_k]
        ]
        return FindingDraft(
            summary=critique.rationale,
            candidates=candidates,
            recommendation=candidates[0].name,
            confidence=0.65 if contents else 0.3,
            classification=FindingClassification.HYPOTHESIS,
            evidence_indices=list(range(len(contents))),
            security_considerations=[
                "Model ranking is not independent verification or authorization"
            ],
            next_steps=["SCOUT must evaluate evidence and create requirements if appropriate"],
        )
