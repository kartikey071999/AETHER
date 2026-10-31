"""Curated unconventional alternatives when model generation/critique is unavailable."""

from aether.agents.scout.schemas import FindingDraft
from aether.communication.contracts import CandidateApproach, ResourceEstimate
from aether.security.untrusted import UntrustedContent
from aether.storage.models import FindingClassification

HEURISTICS: dict[str, tuple[tuple[str, str], ...]] = {
    "audio": (
        ("Signal-processing gate", "Use energy/periodicity features before a local audio model"),
        (
            "Quantized local audio model",
            "Evaluate a compact model with local-only audio processing",
        ),
        ("Specialized audio repository", "Inspect a reviewed narrow-domain audio implementation"),
    ),
    "model": (
        ("Quantized local model", "Compare a small local model with bounded memory requirements"),
        ("Specialized task model", "Replace general generation with a narrow local classifier"),
        ("Retrieval and rules", "Use indexed local evidence and deterministic rules"),
    ),
    "performance": (
        ("Incremental algorithm", "Avoid recomputing unchanged inputs"),
        ("Streaming algorithm", "Bound memory with chunked processing and backpressure"),
        ("Specialized library", "Evaluate a reviewed domain-specific optimized implementation"),
    ),
    "general": (
        (
            "Algorithmic substitute",
            "Replace the standard approach with a simpler deterministic algorithm",
        ),
        (
            "Specialized local repository",
            "Inspect a narrow-domain implementation without installing it",
        ),
        (
            "Failure-driven decomposition",
            "Isolate the failed assumption and solve a smaller subproblem",
        ),
    ),
}


def category(question: str) -> str:
    text = question.lower()
    if any(word in text for word in ("audio", "voice", "microphone", "speech")):
        return "audio"
    if any(word in text for word in ("model", "llm", "inference")):
        return "model"
    if any(word in text for word in ("performance", "slow", "memory", "latency")):
        return "performance"
    return "general"


def alternatives(question: str, contents: list[UntrustedContent], top_k: int) -> FindingDraft:
    candidates = [
        CandidateApproach(
            name=name,
            description=f"Feasibility proposal: {description}",
            pros=["Local-first alternative"],
            cons=["Feasibility and performance are not yet tested"],
            dependencies=["Reviewed implementation or model artifacts"],
            resource_needs=ResourceEstimate(ram_mb=128, disk_mb=64),
            risks=["Artifact provenance, licensing and permissions require review"],
        )
        for name, description in HEURISTICS[category(question)][:top_k]
    ]
    quarantined = any(content.suspicious_patterns() for content in contents)
    return FindingDraft(
        summary=(
            "Instruction-like evidence quarantined; curated alternatives remain untested."
            if quarantined
            else f"Curated {category(question)} alternatives; independent feasibility tests are needed."
        ),
        candidates=candidates,
        recommendation=candidates[0].name,
        confidence=0.2 if quarantined or not contents else 0.4,
        classification=FindingClassification.UNTESTED,
        evidence_indices=list(range(len(contents))),
        security_considerations=["No execution, installation or discovery scan is authorized"],
        next_steps=["Submit alternatives only to SCOUT for consolidation"],
    )
