"""LEDGER's deterministic verification rules: suites, pytest evidence, criteria and verdicts.

Everything here is pure: no database, no tools, no LLM. A capability is verified only when every
acceptance criterion maps to at least one executed test and every mapped test passed. A build or
installation that "succeeded" proves nothing on its own.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from aether.communication.contracts import (
    AcceptanceResult,
    TestCaseResult,
    TestCounts,
    VerificationVerdict,
)
from aether.storage.models import ResultOutcome, RunStatus

MAX_SUITE_TOKENS = 32
MAX_SUITE_CHARS = 200
# Path segments and pytest node ids only: no options, shells, globs or traversal.
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./\\:\[\]@=+-]*$")
_WORKSPACE_TOKEN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./\\-]*$")
_SUMMARY_HEADER = re.compile(r"^=+\s*short test summary info\s*=+$")
_RESULT_LINE = re.compile(
    r"^(?P<outcome>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(?P<rest>\S.*?)\s*$"
)
_SKIPPED_LOCATION = re.compile(r"^\[\d+\]\s+(?P<location>\S+?:\d+)(?::\s*(?P<reason>.*))?$")
_COUNT = re.compile(
    r"(?P<count>\d+)\s+(?P<kind>passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b"
)
_SUMMARY = re.compile(r"\bin\s+\d+(?:\.\d+)?s\b")
_NO_TESTS = re.compile(r"\bno tests ran\b")
_CRITERION_ID = re.compile(r"^\s*(?P<id>[A-Za-z]{1,8}[-_ ]?\d{1,4})\s*[:.)\]-]\s*")

_OUTCOMES = {
    "PASSED": ResultOutcome.PASSED,
    "XPASS": ResultOutcome.PASSED,
    "FAILED": ResultOutcome.FAILED,
    "ERROR": ResultOutcome.ERROR,
    "SKIPPED": ResultOutcome.SKIPPED,
    "XFAIL": ResultOutcome.SKIPPED,
}
_VERDICT_STATUS = {
    VerificationVerdict.VERIFIED: RunStatus.SUCCEEDED,
    VerificationVerdict.FAILED: RunStatus.FAILED,
    VerificationVerdict.INCONCLUSIVE: RunStatus.CANCELLED,
}
_STATUS_VERDICT = {status: verdict for verdict, status in _VERDICT_STATUS.items()}
_FAILING = (ResultOutcome.FAILED, ResultOutcome.ERROR)


class UnsafeSuite(ValueError):
    """The requested suite cannot be expressed as a safe, allowlisted pytest argv."""


@dataclass(frozen=True, slots=True)
class VerificationSpec:
    """Everything LEDGER needs to verify one subject, rebuilt from the durable source message."""

    source_message_id: UUID
    suite: str
    acceptance_criteria: tuple[str, ...]
    task_id: UUID | None = None
    capability_id: UUID | None = None
    requirement_id: UUID | None = None
    objective_id: UUID | None = None
    execution_run_ids: tuple[UUID, ...] = ()
    criterion_tests: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    workspace: str | None = None
    correlation_id: UUID | None = None
    problem: str | None = None

    @property
    def run_id(self) -> UUID:
        # One test run per verification message: redelivery maps onto the same durable run.
        return self.source_message_id

    @property
    def execution_run_id(self) -> UUID | None:
        return self.execution_run_ids[-1] if self.execution_run_ids else None


@dataclass(frozen=True, slots=True)
class ParsedRun:
    cases: tuple[TestCaseResult, ...]
    counts: TestCounts
    summary_found: bool


@dataclass(frozen=True, slots=True)
class CriterionResult:
    criterion: str
    passed: bool
    tests: tuple[str, ...]
    evidence: str
    failed: bool = False

    @property
    def unproven(self) -> bool:
        """Neither passed nor failed: no mapped test ran to a pass or failure (e.g. skipped)."""
        return not self.passed and not self.failed

    def to_acceptance(self) -> AcceptanceResult:
        return AcceptanceResult(
            criterion=self.criterion, passed=self.passed, evidence=self.evidence
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion,
            "passed": self.passed,
            "tests": list(self.tests),
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class Verdict:
    verdict: VerificationVerdict
    reason: str

    @property
    def run_status(self) -> RunStatus:
        return _VERDICT_STATUS[self.verdict]


def verdict_for_status(status: RunStatus | str) -> VerificationVerdict | None:
    """The verdict recorded for a finished test run (``None`` while it is still pending)."""
    try:
        return _STATUS_VERDICT.get(RunStatus(status))
    except ValueError:
        return None


def run_status_for(verdict: VerificationVerdict) -> RunStatus:
    return _VERDICT_STATUS[verdict]


def parse_suite(suite: str) -> tuple[str, ...]:
    """Split a suite into pytest path/node-id tokens, refusing anything that is not one."""
    tokens = tuple(suite.split())
    if not tokens:
        raise UnsafeSuite("the acceptance suite is empty")
    if len(tokens) > MAX_SUITE_TOKENS or len(suite) > MAX_SUITE_CHARS:
        raise UnsafeSuite("the acceptance suite is too long")
    for token in tokens:
        if not _SAFE_TOKEN.match(token):
            raise UnsafeSuite(f"suite token {token[:60]!r} is not a test path or node id")
        path = token.split("::", 1)[0].replace("\\", "/")
        if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
            raise UnsafeSuite("suite paths must be relative to the task workspace")
        if ".." in path.split("/"):
            raise UnsafeSuite("suite paths must not leave the task workspace")
    return tokens


def safe_workspace(value: object) -> str | None:
    """A relative workspace sub-directory, or ``None`` (the policy workspace root)."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip().replace("\\", "/").rstrip("/")
    if not _WORKSPACE_TOKEN.match(value) or ".." in value.split("/"):
        raise UnsafeSuite("the workspace must be a relative path inside the task workspace")
    return value


def pytest_argv(python: str, tokens: Sequence[str]) -> list[str]:
    """``python -m pytest`` with one summary line per test and no captured-output sections.

    ``-rfEsxXp`` (rather than ``-rA``) keeps test stdout out of the report, so a test cannot
    print fake ``PASSED`` lines; the parser also only trusts the final summary section.
    """
    return [
        python,
        "-m",
        "pytest",
        "-q",
        "-rfEsxXp",
        "--tb=short",
        "--color=no",
        "--rootdir=.",
        "-p",
        "no:cacheprovider",
        *tokens,
    ]


def parse_pytest_output(text: str) -> ParsedRun:
    """Per-test outcomes from the last short-summary section plus the final count line."""
    lines = [raw.rstrip() for raw in text.splitlines()]
    start = 0
    for index, line in enumerate(lines):
        if _SUMMARY_HEADER.match(line.strip()):
            start = index + 1
    tail = lines[start:] if start else lines[-1:]
    cases: dict[str, TestCaseResult] = {}
    if start:
        for line in tail:
            match = _RESULT_LINE.match(line.strip())
            if match:
                case = _case(match["outcome"], match["rest"])
                if case is not None:
                    cases.setdefault(case.name, case)
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    summary_found = False
    for line in reversed(tail):
        stripped = line.strip().strip("=").strip()
        if not stripped:
            continue
        if _SUMMARY.search(stripped) and (_COUNT.search(stripped) or _NO_TESTS.search(stripped)):
            summary_found = True
            for item in _COUNT.finditer(stripped):
                kind, count = item["kind"], int(item["count"])
                if kind in ("passed", "xpassed"):
                    counts["passed"] += count
                elif kind == "failed":
                    counts["failed"] += count
                elif kind.startswith("error"):
                    counts["errors"] += count
                elif kind in ("skipped", "xfailed"):
                    counts["skipped"] += count
        break
    return ParsedRun(
        cases=tuple(cases.values()), counts=TestCounts(**counts), summary_found=summary_found
    )


def _case(outcome: str, rest: str) -> TestCaseResult | None:
    if outcome in ("SKIPPED", "XFAIL"):
        located = _SKIPPED_LOCATION.match(rest)
        if located:
            return TestCaseResult(
                name=_short(located["location"]),
                outcome=_OUTCOMES[outcome],
                message=(located["reason"] or None),
            )
    name, separator, detail = rest.partition(" - ")
    message = detail.strip()[:1000] or None if separator else None
    name = name.strip()
    if not name:
        return None
    return TestCaseResult(name=_short(name), outcome=_OUTCOMES[outcome], message=message)


def _short(name: str) -> str:
    return name if len(name) <= 500 else name[-500:]


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def criterion_keys(criterion: str) -> tuple[str, ...]:
    """Slugs a test name must contain to count as evidence for ``criterion``.

    ``"AC1: adds numbers"`` matches tests named ``test_ac1_...`` or ``test_adds_numbers``.
    """
    keys: list[str] = []
    identifier = _CRITERION_ID.match(criterion)
    if identifier:
        keys.append(re.sub(r"[^a-z0-9]", "", identifier["id"].lower()))
        remainder = _normalise(criterion[identifier.end() :])
        if remainder:
            keys.append(remainder)
    full = _normalise(criterion)
    if full:
        keys.append(full)
    return tuple(dict.fromkeys(keys))


def _test_function(name: str) -> str:
    return _normalise(name.rsplit("::", 1)[-1]) if "::" in name else _normalise(name)


def _contains_key(test_name: str, key: str) -> bool:
    function = _test_function(test_name)
    # Whole-word match so "ac1" does not claim "test_ac10" as evidence.
    return re.search(rf"(?:^|_){re.escape(key)}(?:_|$)", function) is not None


def _matches_selector(test_name: str, selector: str) -> bool:
    name = test_name.replace("\\", "/")
    selector = selector.replace("\\", "/")
    if name == selector or name.endswith("/" + selector):
        return True
    if "::" not in selector:
        # A bare function name selects that test (and its parametrisations).
        function = name.rsplit("::", 1)[-1]
        return function == selector or function.startswith(selector + "[")
    return name.startswith(selector + "[") or name.endswith("/" + selector.split("[", 1)[0])


def explicit_criterion_tests(evidence: Iterable[Mapping[str, Any]]) -> dict[str, tuple[str, ...]]:
    """``{"criterion": ..., "tests": [...]}`` evidence items naming the tests for a criterion."""
    mapping: dict[str, tuple[str, ...]] = {}
    for item in evidence:
        criterion = item.get("criterion")
        tests = item.get("tests")
        if not isinstance(criterion, str) or not isinstance(tests, list):
            continue
        names = tuple(test.strip() for test in tests if isinstance(test, str) and test.strip())
        if criterion.strip() and names:
            mapping[criterion.strip().casefold()] = names
    return mapping


def evaluate_criteria(
    criteria: Sequence[str],
    cases: Sequence[TestCaseResult],
    criterion_tests: Mapping[str, Sequence[str]] | None = None,
) -> list[CriterionResult]:
    """Map each criterion to executed tests; it passes only if all of them passed."""
    explicit = criterion_tests or {}
    results: list[CriterionResult] = []
    for criterion in criteria:
        selectors = explicit.get(criterion.strip().casefold())
        if selectors:
            matched = [
                case for case in cases if any(_matches_selector(case.name, s) for s in selectors)
            ]
            how = "explicit test mapping"
        else:
            keys = criterion_keys(criterion)
            matched = [case for case in cases if any(_contains_key(case.name, k) for k in keys)]
            how = "test name match"
        names = tuple(case.name for case in matched)
        if not matched:
            results.append(
                CriterionResult(
                    criterion=criterion,
                    passed=False,
                    tests=(),
                    evidence="no executed test provides evidence for this criterion",
                )
            )
            continue
        broken = [case for case in matched if case.outcome in _FAILING]
        not_passed = [case for case in matched if case.outcome is not ResultOutcome.PASSED]
        if not_passed:
            detail = ", ".join(f"{case.name} {case.outcome.value}" for case in not_passed[:5])
        else:
            detail = f"{len(matched)} test(s) passed: " + ", ".join(names[:5])
        results.append(
            CriterionResult(
                criterion=criterion,
                passed=not not_passed,
                tests=names,
                evidence=f"{how}: {detail}"[:1000],
                failed=bool(broken),
            )
        )
    return results


def decide_verdict(
    parsed: ParsedRun,
    criteria: Sequence[CriterionResult],
    *,
    exit_code: int | None,
    timed_out: bool,
) -> Verdict:
    """VERIFIED only on a clean run where every criterion has passing test evidence."""
    counts = parsed.counts
    if timed_out:
        return Verdict(VerificationVerdict.INCONCLUSIVE, "the acceptance suite timed out")
    broken = failing_tests(parsed.cases)
    failing_criteria = [result for result in criteria if result.failed]
    if counts.failed + counts.errors > 0 or broken or failing_criteria:
        names = "; ".join(result.criterion for result in failing_criteria[:5]) or "none mapped"
        return Verdict(
            VerificationVerdict.FAILED,
            f"{counts.failed} failed, {counts.errors} error(s); failing criteria: {names}",
        )
    if not parsed.summary_found:
        return Verdict(VerificationVerdict.INCONCLUSIVE, "pytest output could not be parsed")
    if exit_code not in (0, None):
        return Verdict(
            VerificationVerdict.INCONCLUSIVE,
            f"pytest exited with code {exit_code} without reporting failing tests",
        )
    if counts.passed == 0:
        return Verdict(VerificationVerdict.INCONCLUSIVE, "no acceptance test was executed")
    if not criteria:
        return Verdict(VerificationVerdict.INCONCLUSIVE, "no acceptance criteria were defined")
    unproven = [result.criterion for result in criteria if result.unproven]
    if unproven:
        return Verdict(
            VerificationVerdict.INCONCLUSIVE,
            "criteria without passing test evidence: " + "; ".join(unproven[:5]),
        )
    return Verdict(VerificationVerdict.VERIFIED, "every acceptance criterion passed with evidence")


def failing_tests(cases: Iterable[TestCaseResult]) -> list[str]:
    return [case.name for case in cases if case.outcome in _FAILING]


def merged_counts(parsed: ParsedRun) -> TestCounts:
    """Summary counts, never lower than the per-test lines actually reported."""
    tally = {outcome: 0 for outcome in ResultOutcome}
    for case in parsed.cases:
        tally[case.outcome] += 1
    counts = parsed.counts
    return TestCounts(
        passed=max(counts.passed, tally[ResultOutcome.PASSED]),
        failed=max(counts.failed, tally[ResultOutcome.FAILED]),
        errors=max(counts.errors, tally[ResultOutcome.ERROR]),
        skipped=max(counts.skipped, tally[ResultOutcome.SKIPPED]),
    )
