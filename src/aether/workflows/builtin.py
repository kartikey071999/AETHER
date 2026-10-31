"""Built-in workflows: shipped, verified flows agents run instead of hand-coding the steps."""

from aether.communication.messages import AgentId
from aether.workflows.definition import WorkflowDefinition, WorkflowStep

RESEARCH_BASIC = WorkflowDefinition(
    name="research.basic",
    version=1,
    owner=AgentId.SCOUT,
    description=(
        "Gather allowlisted sources, screen them for injected instructions, and summarise only "
        "the clean ones for a research question (model with deterministic fallback)."
    ),
    inputs={"question": "str", "urls": "list"},
    steps=[
        WorkflowStep(id="gather", skill="web.gather", inputs={"urls": "${input.urls}"}),
        WorkflowStep(
            id="screen",
            skill="text.screen_untrusted",
            inputs={"sources": "${steps.gather.output.sources}"},
        ),
        WorkflowStep(
            id="summarise",
            skill="llm.summarise",
            when="${steps.screen.output.clean}",
            inputs={"question": "${input.question}", "sources": "${steps.screen.output.clean}"},
        ),
    ],
    outputs={
        "summary": "${steps.summarise.output.summary}",
        "key_points": "${steps.summarise.output.key_points}",
        "method": "${steps.summarise.output.method}",
        "cited_hashes": "${steps.summarise.output.cited_hashes}",
        "flagged": "${steps.screen.output.flagged}",
        "failed_urls": "${steps.gather.output.failed}",
    },
)


def builtin_workflows() -> list[WorkflowDefinition]:
    return [RESEARCH_BASIC]
