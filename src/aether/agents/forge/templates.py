"""Deterministic capability templates: known capabilities FORGE builds without an LLM.

A template turns a recognised capability request into a :class:`ForgeSpec`, so the plan is the
same reviewed ``python_test`` plan every time. LLM drafting stays an optional refinement
(``ForgeConfig.llm_refine_templates``); when a draft is invalid, empty or truncated, FORGE falls
back to the template instead of reporting a blocker.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from aether.agents.forge.schemas import ForgeSpec

TEXT_STATISTICS_MODULE_FILE = "text_statistics.py"
TEXT_STATISTICS_TEST_FILE = "test_text_statistics.py"

TEXT_STATISTICS_MODULE = '''"""text_statistics: word, line and character counts for a UTF-8 string."""

import json
import sys


def text_statistics(text: str) -> dict[str, int]:
    return {
        "words": len(text.split()),
        "lines": len(text.splitlines()),
        "characters": len(text),
    }


def to_json(text: str) -> str:
    return json.dumps(text_statistics(text), sort_keys=True)


if __name__ == "__main__":
    print(to_json(" ".join(sys.argv[1:]) or "a b c"))
'''

TEXT_STATISTICS_TESTS = '''"""Acceptance tests for text_statistics; test names carry the criterion ids (AC1..AC3)."""

import json

from text_statistics import text_statistics, to_json


def test_ac1_counts_three_words_for_a_b_c():
    assert text_statistics("a b c")["words"] == 3


def test_ac2_empty_string_reports_zeros():
    assert text_statistics("") == {"words": 0, "lines": 0, "characters": 0}


def test_ac3_non_ascii_counted_by_code_points():
    stats = text_statistics("na\\u00efve caf\\u00e9 \\u65e5\\u672c")
    assert stats["characters"] == 13
    assert stats["words"] == 3
    assert json.loads(to_json("\\u00e9"))["characters"] == 1
'''


@dataclass(frozen=True, slots=True)
class CapabilityTemplate:
    name: str
    pattern: re.Pattern[str]
    build: Callable[[], ForgeSpec]


def _text_statistics() -> ForgeSpec:
    return ForgeSpec(
        kind="python_test",
        files={
            TEXT_STATISTICS_MODULE_FILE: TEXT_STATISTICS_MODULE,
            TEXT_STATISTICS_TEST_FILE: TEXT_STATISTICS_TESTS,
        },
        command=["-m", "text_statistics", "a b c"],
    )


TEMPLATES: tuple[CapabilityTemplate, ...] = (
    CapabilityTemplate(
        "text_statistics", re.compile(r"\btext_statistics\b", re.IGNORECASE), _text_statistics
    ),
)


def match_template(*texts: str | None) -> tuple[str, ForgeSpec] | None:
    """The first capability template named by any of ``texts`` (task or requirement text)."""
    for template in TEMPLATES:
        if any(template.pattern.search(text or "") for text in texts):
            return template.name, template.build()
    return None
