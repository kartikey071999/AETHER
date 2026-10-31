import pytest
from pydantic import SecretStr, ValidationError

from aether.models import ProviderSettings, provider_hosts


def test_exact_env_names_and_secret_wrapping(monkeypatch):
    entries = {
        "GROQ_API_KEY": "test-groq",
        "AZURE_OPENAI_ENDPOINT": "https://azure.example.com",
        "AZURE_OPEN_AI_KEY": "test-azure",
        "OPENAI_API_VERSION": "2026-01-01",
        "ANTHROPIC_ENDPOINT": "https://opus.example.com/anthropic",
        "ANTHROPIC_API_KEY": "test-anthropic",
        "API_VERSION": "2023-06-01",
        "OLLAMA_BASE_URL": "http://localhost:11434",
    }
    for name, value in entries.items():
        monkeypatch.setenv(name, value)
    settings = ProviderSettings(_env_file=None)
    for field in (
        "groq_api_key",
        "azure_openai_endpoint",
        "azure_openai_key",
        "openai_api_version",
        "anthropic_endpoint",
        "anthropic_api_key",
        "anthropic_api_version",
        "ollama_base_url",
    ):
        assert isinstance(getattr(settings, field), SecretStr)
    assert not any(value in repr(settings) for value in entries.values())
    assert provider_hosts(settings) == {
        "api.groq.com",
        "azure.example.com",
        "opus.example.com",
        "localhost",
    }


def test_empty_credentials_are_unavailable(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  ")
    settings = ProviderSettings(_env_file=None)
    assert settings.groq_api_key is None and settings.anthropic_api_key is None


def test_partial_budget_env_preserves_azure_defaults(monkeypatch):
    monkeypatch.setenv("AETHER_PROVIDER_BUDGETS", '{"azure_openai":{"max_requests_per_day":3}}')
    settings = ProviderSettings(_env_file=None)
    assert settings.budgets["azure_openai"].max_requests_per_day == 3
    assert settings.budgets["azure_openai"].max_tokens_per_day == 3_000_000
    assert settings.budgets["azure_openai"].max_concurrent_requests == 4
    assert settings.budgets["azure_openai"].allow_unpriced
    assert settings.budgets["groq"].max_requests_per_day == 2000


def test_azure_defaults_allow_real_use_bounded_by_request_and_token_caps():
    budgets = ProviderSettings(_env_file=None).budgets
    astra, opus = budgets["azure_openai"], budgets["azure_anthropic"]
    assert (astra.max_requests_per_day, astra.max_tokens_per_day) == (1000, 3_000_000)
    assert (astra.max_concurrent_requests, astra.max_tokens_per_request) == (4, 8192)
    assert (opus.max_requests_per_day, opus.max_tokens_per_day) == (300, 1_500_000)
    assert (opus.max_concurrent_requests, opus.max_tokens_per_request) == (2, 16_384)
    assert astra.allow_unpriced and opus.allow_unpriced
    assert not budgets["groq"].allow_unpriced
    assert budgets["groq"].max_requests_per_day == 2000


@pytest.mark.parametrize("cap", ["max_requests_per_day", "max_tokens_per_day"])
def test_unpriced_providers_must_keep_daily_request_and_token_caps(cap):
    with pytest.raises(ValidationError, match="allow_unpriced requires"):
        ProviderSettings(_env_file=None, budgets={"azure_openai": {cap: None}})
    with pytest.raises(ValidationError, match="allow_unpriced requires"):
        ProviderSettings(_env_file=None, budgets={"groq": {"allow_unpriced": True, cap: None}})


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com",
        "https://user:private-value@example.com",
        "https://example.com?key=private-value",
        "http://remote.example.com",
    ],
)
def test_invalid_or_secret_endpoint_rejected_without_echo(url):
    with pytest.raises(ValidationError) as captured:
        ProviderSettings(_env_file=None, AZURE_OPENAI_ENDPOINT=url)
    assert "private-value" not in str(captured.value)


def test_safe_host_defaults_do_not_change_policy(settings):
    assert provider_hosts(settings) == {"localhost"}
    assert settings.ollama_base_url.get_secret_value() == "http://localhost:11434"
