"""Pure secret redaction for durable records, spool entries, and log output."""

import re
from typing import Any

REDACTED = "***"

_SECRET_KEY = re.compile(
    r"(?:^|[_\-.])"
    r"(?:key|token|secret|password|authorization|api_key|dsn|database_url)"
    r"(?:$|[_\-.])",
    re.IGNORECASE,
)
_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<credentials>[^/\s@]+)@", re.IGNORECASE
)
_BEARER = re.compile(r"\b(Bearer)\s+[a-z0-9._~+/=-]+", re.IGNORECASE)
_AUTHORIZATION = re.compile(
    r"\b(authorization\s*[:=]\s*)(?:Bearer|Basic)\s+[a-z0-9._~+/=-]+", re.IGNORECASE
)
_TOKEN = re.compile(r"\b(?:gh[pousr]_|github_pat_|gsk_|sk-)[a-z0-9_-]+", re.IGNORECASE)
_ASSIGNMENT = re.compile(
    r"""(?P<label>\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|
    authorization|dsn|database[_-]?url)\b\s*[:=]\s*)
    (?P<value>"[^"]*"|'[^']*'|[^\s,;]+)""",
    re.IGNORECASE | re.VERBOSE,
)


def is_secret_key(key: str) -> bool:
    normalized = _CAMEL_BOUNDARY.sub(r"\1_\2", _ACRONYM_BOUNDARY.sub(r"\1_\2", key))
    return _SECRET_KEY.search(normalized) is not None


def redact_string(value: str) -> str:
    def redact_credentials(match: re.Match[str]) -> str:
        credentials = match["credentials"]
        username = credentials.partition(":")[0] if ":" in credentials else REDACTED
        username = _TOKEN.sub(REDACTED, username)
        return f"{match['scheme']}{username}:{REDACTED}@"

    value = _URL_CREDENTIALS.sub(redact_credentials, value)
    value = _AUTHORIZATION.sub(lambda match: f"{match[1]}{REDACTED}", value)
    value = _BEARER.sub(lambda match: f"{match[1]} {REDACTED}", value)
    value = _TOKEN.sub(REDACTED, value)
    return _ASSIGNMENT.sub(lambda match: f"{match['label']}{REDACTED}", value)


def redact(value: Any) -> Any:
    """Return a redacted copy; numeric token counts (``tokens``) are not secrets."""
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, dict):
        return {
            redact_string(key) if isinstance(key, str) else key: (
                REDACTED if isinstance(key, str) and is_secret_key(key) else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value
