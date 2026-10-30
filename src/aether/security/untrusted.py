"""Untrusted external content: data to inspect, never instructions to follow.

``UntrustedContent`` can be summarised, quoted, and attached to WARDEN evidence, but this
module deliberately offers no conversion into an ``Operation``, an approval, or a policy change.
"""

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Heuristic markers of instruction-like text aimed at agents. Flags are evidence for WARDEN,
# not a security boundary: absence of a flag says nothing about safety.
SUSPICIOUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE | re.DOTALL))
    for name, pattern in (
        ("ignore_instructions", r"\b(ignore|disregard|forget)\b.{0,40}\binstructions?\b"),
        (
            "approval_claim",
            r"\b(warden|security|policy)\b.{0,40}\b(approves?|approved|authori[sz]e[sd]?)\b",
        ),
        (
            "blanket_grant",
            r"\b(approves?|allows?|grants?|authori[sz]es?)\b.{0,20}\b(all|everything|anything)\b",
        ),
        (
            "policy_override",
            (
                r"\b(disable|bypass|override|turn off)\b.{0,40}\b(security|polic(y|ies)|"
                r"warden|sandbox|allowlist|restrictions?)\b"
            ),
        ),
        ("role_reassignment", r"\b(you are now|act as|new system prompt|system prompt)\b"),
        ("pipe_to_shell", r"\b(curl|wget|iwr|invoke-webrequest)\b[^\n|]{0,200}\|\s*(ba|z)?sh\b"),
        ("destructive_command", r"(\brm\s+-rf\b|\bdel\s+/[fsq]\b|\bformat\s+[a-z]:)"),
        (
            "credential_request",
            (
                r"\b(send|print|reveal|exfiltrate)\b.{0,40}"
                r"\b(token|secret|password|api key|credentials?)\b"
            ),
        ),
    )
)
_EXCERPT_LIMIT = 500


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class UntrustedContent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1, max_length=2048)
    content: str
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    content_hash: str = ""

    @field_validator("retrieved_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("retrieved_at must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _bind_hash(self) -> "UntrustedContent":
        expected = content_sha256(self.content)
        if not self.content_hash:
            object.__setattr__(self, "content_hash", expected)
        elif self.content_hash != expected:
            raise ValueError("content_hash does not match content")
        return self

    def suspicious_patterns(self) -> tuple[str, ...]:
        return tuple(name for name, pattern in SUSPICIOUS_PATTERNS if pattern.search(self.content))

    def excerpt(self, limit: int = _EXCERPT_LIMIT) -> str:
        if len(self.content) <= limit:
            return self.content
        return self.content[: max(limit - 3, 0)] + "..."

    def quote(self, limit: int = _EXCERPT_LIMIT) -> str:
        """Delimited excerpt for prompts or reports; the frame marks it as data, not commands."""
        return (
            f"<<untrusted source={self.source!r} sha256={self.content_hash[:16]}>>\n"
            f"{self.excerpt(limit)}\n<<end untrusted>>"
        )

    def summary(self) -> str:
        flags = self.suspicious_patterns()
        flagged = f", flagged: {', '.join(flags)}" if flags else ""
        return f"untrusted content from {self.source} ({len(self.content)} chars{flagged})"

    def as_evidence(self, limit: int = _EXCERPT_LIMIT) -> dict[str, Any]:
        """Evidence entry for a WARDEN decision; content stays quoted data."""
        return {
            "type": "untrusted_content",
            "source": self.source,
            "content_hash": self.content_hash,
            "retrieved_at": self.retrieved_at.isoformat(),
            "length": len(self.content),
            "suspicious_patterns": list(self.suspicious_patterns()),
            "excerpt": self.excerpt(limit),
        }
