"""The seeded SCOUT finding comparing web extraction libraries (docs/web-research.md)."""

import json
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from aether.agents.scout.schemas import FindingDraft
from aether.storage.database import Database
from aether.storage.models import FindingEvidenceRecord, ResearchFindingRecord
from aether.storage.repositories import ResearchFindingRepository
from aether.storage.repositories.research import EvidenceInput

FIXTURE = Path(__file__).parents[2] / "fixtures" / "research" / "web_research_tools_finding.json"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def load() -> tuple[dict, FindingDraft, list[EvidenceInput]]:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    draft = FindingDraft.model_validate(document["draft"])
    evidence = [
        EvidenceInput(
            source_uri=item["source_uri"],
            summary=item["summary"],
            content_hash=item["content_hash"],
            retrieved_at=datetime.fromisoformat(item["retrieved_at"])
            if item["retrieved_at"]
            else None,
        )
        for item in document["evidence"]
    ]
    return document, draft, evidence


def test_seeded_finding_is_a_valid_cited_scout_draft() -> None:
    document, draft, evidence = load()
    assert document["agent"] == "SCOUT"
    assert draft.recommendation == "stdlib html.parser"
    assert sorted(draft.evidence_indices) == list(range(len(evidence)))
    for item in evidence:
        assert item.source_uri.startswith("https://")
        assert item.content_hash is None or SHA256.match(item.content_hash)
    hashed = [item for item in evidence if item.content_hash]
    assert hashed and all(item.retrieved_at is not None for item in hashed)
    assert all(item.source_uri.startswith("https://pypi.org/pypi/") for item in hashed)


async def test_seeded_finding_persists_with_evidence(database: Database, objective_id) -> None:
    document, draft, evidence = load()
    finding_id = await ResearchFindingRepository(database.sessions).submit(
        agent=document["agent"],
        objective_id=objective_id,
        question=document["question"],
        findings=draft.summary,
        confidence=draft.confidence,
        classification=draft.classification,
        candidate_approaches=[item.model_dump(mode="json") for item in draft.candidates],
        recommendation=draft.recommendation,
        evidence=[evidence[index] for index in draft.evidence_indices],
    )
    async with database.sessions() as session:
        finding = await session.get(ResearchFindingRecord, finding_id)
        stored = list(
            await session.scalars(
                select(FindingEvidenceRecord).where(FindingEvidenceRecord.finding_id == finding_id)
            )
        )
    assert finding is not None and finding.recommendation == "stdlib html.parser"
    assert {item.source_uri: item.content_hash for item in stored} == {
        item.source_uri: item.content_hash for item in evidence
    }
