from copy import deepcopy

import pytest

from aether.observability.redaction import is_secret_key, redact, redact_string


@pytest.mark.parametrize(
    "key",
    [
        "key",
        "TOKEN",
        "secret",
        "password",
        "authorization",
        "api_key",
        "dsn",
        "database_url",
        "access_token",
        "client-secret",
        "encryption.key",
        "ApiKey",
        "databaseUrl",
        "clientSecret",
        "x-api-key",
        "PASSWORD_HASH",
        "APIKey",
    ],
)
def test_secret_keys_are_redacted(key: str) -> None:
    assert is_secret_key(key)
    assert redact({key: {"nested": "sensitive"}}) == {key: "***"}


@pytest.mark.parametrize("key", ["tokens", "input_tokens", "output_tokens", "monkey", "task_id"])
def test_nonsecret_keys_and_token_measurements_are_preserved(key: str) -> None:
    assert not is_secret_key(key)
    assert redact({key: 123}) == {key: 123}


@pytest.mark.parametrize(
    "source,expected",
    [
        ("postgresql://user:password@localhost/db", "postgresql://user:***@localhost/db"),
        ("postgresql+asyncpg://user:p%40ss@host/db", "postgresql+asyncpg://user:***@host/db"),
        ("https://user@host/path", "https://***:***@host/path"),
        ("Bearer abc.def_xyz-123", "Bearer ***"),
        ("bearer abc+/==", "bearer ***"),
        ("ghp_fake123 gho_fake456 sk-fake789 github_pat_fake", "*** *** *** ***"),
        ("groq key gsk_fakeGroqKey123", "groq key ***"),
        ("password='two words' token=abcdef", "password=*** token=***"),
        ('API_KEY: "two words"', "API_KEY: ***"),
        ("Authorization: Basic dXNlcjpwYXNz", "Authorization: ***"),
        ("Authorization: Bearer abc123", "Authorization: ***"),
        ("safe https://example.invalid/path", "safe https://example.invalid/path"),
    ],
)
def test_string_redaction(source: str, expected: str) -> None:
    assert redact_string(source) == expected
    assert redact_string(expected) == expected


def test_redaction_is_recursive_and_does_not_mutate_input() -> None:
    original = {
        "items": [{"password": "hidden"}, {"message": "Bearer abc123"}],
        "tuple": ("ghp_fake", {"dsn": "hidden"}),
        "safe": None,
        "count": 10,
    }
    before = deepcopy(original)
    assert redact(original) == {
        "items": [{"password": "***"}, {"message": "Bearer ***"}],
        "tuple": ("***", {"dsn": "***"}),
        "safe": None,
        "count": 10,
    }
    assert original == before


def test_secret_strings_in_dictionary_keys_are_redacted() -> None:
    assert redact({"ghp_fake123": "safe"}) == {"***": "safe"}
