"""Pure verification rules: suites, pytest parsing, criteria mapping and verdicts."""

import pytest

from aether.agents.ledger.workflow import (
    UnsafeSuite,
    criterion_keys,
    decide_verdict,
    evaluate_criteria,
    explicit_criterion_tests,
    merged_counts,
    parse_pytest_output,
    parse_suite,
    pytest_argv,
    safe_workspace,
)
from aether.communication.contracts import TestCaseResult, VerificationVerdict
from aether.storage.models import ResultOutcome

REAL_OUTPUT = """\
..F.s                                                                    [100%]
=================================== FAILURES ===================================
___________________________ test_ac2_rejects_text ____________________________
----------------------------- Captured stdout call -----------------------------
=========================== short test summary info ============================
PASSED t.py::test_ac2_rejects_text
1 passed in 0.01s
=========================== short test summary info ============================
PASSED t.py::test_ac1_adds
PASSED t.py::test_ac3_lists
FAILED t.py::test_ac2_rejects_text - AssertionError: accepted text
SKIPPED [1] t.py:20: not implemented
1 failed, 2 passed, 1 skipped in 0.08s
"""


def case(name: str, outcome: ResultOutcome) -> TestCaseResult:
    return TestCaseResult(name=name, outcome=outcome)


def test_parser_trusts_only_the_final_summary_section():
    parsed = parse_pytest_output(REAL_OUTPUT)

    outcomes = {item.name: item.outcome for item in parsed.cases}
    assert outcomes == {
        "t.py::test_ac1_adds": ResultOutcome.PASSED,
        "t.py::test_ac3_lists": ResultOutcome.PASSED,
        "t.py::test_ac2_rejects_text": ResultOutcome.FAILED,
        "t.py:20": ResultOutcome.SKIPPED,
    }
    assert parsed.summary_found
    assert (parsed.counts.passed, parsed.counts.failed, parsed.counts.skipped) == (2, 1, 1)


def test_parser_without_summary_is_not_evidence():
    parsed = parse_pytest_output("PASSED t.py::test_ac1_adds\n(output truncated)")
    assert parsed.cases == ()
    assert not parsed.summary_found


def test_parser_reads_errors_and_no_tests():
    parsed = parse_pytest_output(
        "=== short test summary info ===\nERROR t.py - ImportError\n1 error in 0.1s\n"
    )
    assert parsed.counts.errors == 1
    assert parse_pytest_output("\nno tests ran in 0.01s\n").summary_found


def test_criterion_keys_use_identifier_and_wording():
    assert criterion_keys("AC1: adds two numbers") == (
        "ac1",
        "adds_two_numbers",
        "ac1_adds_two_numbers",
    )
    assert criterion_keys("Lists every widget") == ("lists_every_widget",)


def test_criteria_need_every_mapped_test_to_pass():
    cases = [
        case("t.py::test_ac1_adds", ResultOutcome.PASSED),
        case("t.py::test_ac1_adds_negative", ResultOutcome.FAILED),
        case("t.py::test_ac10_other", ResultOutcome.PASSED),
    ]
    (ac1,) = evaluate_criteria(["AC1: adds"], cases)
    assert not ac1.passed and ac1.failed
    assert "t.py::test_ac10_other" not in ac1.tests


def test_explicit_mapping_overrides_name_matching():
    mapping = explicit_criterion_tests(
        [{"criterion": "Handles unicode", "tests": ["t.py::test_text"]}, {"bogus": 1}]
    )
    cases = [case("t.py::test_text", ResultOutcome.PASSED)]
    (result,) = evaluate_criteria(["Handles unicode"], cases, mapping)
    assert result.passed
    assert result.tests == ("t.py::test_text",)


def _verdict(cases, criteria, *, exit_code=0, timed_out=False):
    output = "=== short test summary info ===\n" + "\n".join(
        f"{item.outcome.value.upper()} {item.name}" for item in cases
    )
    passed = sum(item.outcome is ResultOutcome.PASSED for item in cases)
    failed = sum(item.outcome is ResultOutcome.FAILED for item in cases)
    output += f"\n{failed} failed, {passed} passed in 0.1s\n"
    parsed = parse_pytest_output(output)
    results = evaluate_criteria(criteria, parsed.cases)
    return decide_verdict(parsed, results, exit_code=exit_code, timed_out=timed_out)


def test_verdicts():
    passing = [case("t.py::test_ac1_x", ResultOutcome.PASSED)]
    assert _verdict(passing, ["AC1: x"]).verdict is VerificationVerdict.VERIFIED
    assert _verdict(passing, ["AC1: x", "AC2: y"]).verdict is VerificationVerdict.INCONCLUSIVE
    assert _verdict(passing, []).verdict is VerificationVerdict.INCONCLUSIVE
    assert _verdict(passing, ["AC1: x"], timed_out=True).verdict is VerificationVerdict.INCONCLUSIVE
    assert _verdict(passing, ["AC1: x"], exit_code=2).verdict is VerificationVerdict.INCONCLUSIVE
    failing = [*passing, case("t.py::test_other", ResultOutcome.FAILED)]
    # A failure anywhere in the suite fails verification, even outside the mapped tests.
    assert _verdict(failing, ["AC1: x"], exit_code=1).verdict is VerificationVerdict.FAILED


def test_merged_counts_never_undercount_reported_lines():
    parsed = parse_pytest_output(
        "=== short test summary info ===\nFAILED t.py::test_a - boom\nFAILED t.py::test_b\n"
        "1 failed in 0.1s\n"
    )
    assert merged_counts(parsed).failed == 2


@pytest.mark.parametrize(
    "suite",
    [
        "--rootdir=/",
        "-p evil",
        "t.py; rm -rf /",
        "/abs/test.py",
        "C:\\abs\\test.py",
        "../outside/test.py",
        "tests/../../x.py",
        "t.py|sh",
        "",
    ],
)
def test_unsafe_suites_are_refused(suite):
    with pytest.raises(UnsafeSuite):
        parse_suite(suite)


def test_safe_suite_builds_fixed_argv():
    tokens = parse_suite("tests/test_widget.py tests/test_more.py::test_ac1[param-1]")
    argv = pytest_argv("python", tokens)
    assert argv[:3] == ["python", "-m", "pytest"]
    assert argv[-2:] == ["tests/test_widget.py", "tests/test_more.py::test_ac1[param-1]"]
    assert "-rA" not in argv


def test_workspace_must_stay_inside():
    assert safe_workspace(None) is None
    assert safe_workspace("tasks/abc/") == "tasks/abc"
    for bad in ("../x", "/etc", "C:/x", "a/../../b"):
        with pytest.raises(UnsafeSuite):
            safe_workspace(bad)
