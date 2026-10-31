"""Deterministic failure classification and routing; no LLM and no blind repeated fixes."""

import re

from aether.agents.forge.schemas import FailureClass, FailureRoute
from aether.communication.contracts import BlockerKind
from aether.communication.messages import AgentId

_PATTERNS: tuple[tuple[FailureClass, re.Pattern[str]], ...] = (
    (
        FailureClass.MISSING_DEPENDENCY,
        re.compile(
            r"ModuleNotFoundError|No module named|ImportError|cannot import name|"
            r"command not found|is not recognized as|No matching distribution|"
            r"Could not find a version|executable file not found",
            re.IGNORECASE,
        ),
    ),
    (
        FailureClass.ENVIRONMENT,
        re.compile(
            r"timed out|timeout|ConnectionError|Connection (reset|refused|aborted)|"
            r"Temporary failure|Max retries exceeded|Name or service not known|"
            r"database is locked|No space left|MemoryError|Too many open files|"
            r"PermissionError|Access is denied|resource temporarily unavailable|"
            r"locked by another process|ProxyError|SSLError",
            re.IGNORECASE,
        ),
    ),
    (
        FailureClass.CODE_DEFECT,
        re.compile(
            r"AssertionError|SyntaxError|IndentationError|NameError|TypeError|ValueError|"
            r"AttributeError|KeyError|IndexError|ZeroDivisionError|FAILED|"
            r"FileNotFoundError|is not a readable file",
            re.IGNORECASE,
        ),
    ),
)

ROUTES: dict[FailureClass, FailureRoute] = {
    FailureClass.ENVIRONMENT: FailureRoute(BlockerKind.RESOURCE, AgentId.ORCHESTR),
    FailureClass.MISSING_DEPENDENCY: FailureRoute(BlockerKind.RESEARCH, AgentId.SCOUT),
    FailureClass.UNKNOWN: FailureRoute(BlockerKind.RESEARCH, AgentId.SCOUT),
    FailureClass.CODE_DEFECT: FailureRoute(BlockerKind.IMPLEMENTATION, AgentId.ORCHESTR),
    FailureClass.PLAN_DEFECT: FailureRoute(BlockerKind.IMPLEMENTATION, AgentId.ORCHESTR),
    FailureClass.SECURITY: FailureRoute(BlockerKind.SECURITY, AgentId.WARDEN),
}


def classify_failure(text: str, *, timed_out: bool = False) -> FailureClass:
    """Classify tool output/error text; a timeout is always an environment failure."""
    if timed_out:
        return FailureClass.ENVIRONMENT
    for failure_class, pattern in _PATTERNS:
        if pattern.search(text or ""):
            return failure_class
    return FailureClass.UNKNOWN


def is_retryable(failure_class: FailureClass) -> bool:
    """Only environment/configuration trouble is worth a bounded retry with backoff."""
    return failure_class is FailureClass.ENVIRONMENT


def route_for(failure_class: FailureClass) -> FailureRoute:
    return ROUTES[failure_class]
