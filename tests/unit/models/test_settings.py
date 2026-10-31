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
    assert settings.budgets["azure_openai"].max_tokens_per_day is None
    assert settings.budgets["azure_openai"].max_concurrent_requests == 8
    # Groq quotas are per model; the provider-wide policy has no daily caps of its own.
    assert settings.budgets["groq"].max_requests_per_day is None
    assert settings.model_budgets["groq/openai/gpt-oss-120b"].max_requests_per_day == 1000


def test_azure_has_no_usage_limits_but_groq_keeps_its_budget():
    budgets = ProviderSettings(_env_file=None).budgets
    for azure in (budgets["azure_openai"], budgets["azure_anthropic"]):
        assert azure.max_requests_per_day is None and azure.max_tokens_per_day is None
        assert azure.max_cost_usd_per_day is None and not azure.allow_unpriced
        assert (azure.max_concurrent_requests, azure.max_tokens_per_request) == (8, 32_768)
    groq = budgets["groq"]
    assert (groq.max_requests_per_day, groq.max_tokens_per_day) == (None, None)
    assert groq.max_cost_usd_per_day == 10 and not groq.allow_unpriced


# The user's Groq account limits: (requests/minute, requests/day, tokens/minute, tokens/day).
GROQ_LIMITS = {
    "allam-2-7b": (30, 7_000, 6_000, 500_000),
    "meta-llama/llama-prompt-guard-2-22m": (30, 14_400, 15_000, 500_000),
    "meta-llama/llama-prompt-guard-2-86m": (30, 14_400, 15_000, 500_000),
    "openai/gpt-oss-120b": (30, 1_000, 8_000, 200_000),
    "openai/gpt-oss-20b": (30, 1_000, 8_000, 200_000),
    "openai/gpt-oss-safeguard-20b": (3, 1_000, 2_000, 200_000),
    "qwen/qwen3.8-27b": (30, 1_000, 8_000, 200_000),
}


@pytest.mark.parametrize(("model", "limits"), GROQ_LIMITS.items())
def test_groq_model_budgets_match_the_account_limits(model, limits):
    policy = ProviderSettings(_env_file=None).model_budgets[f"groq/{model}"]
    assert (
        policy.max_requests_per_minute,
        policy.max_requests_per_day,
        policy.max_tokens_per_minute,
        policy.max_tokens_per_day,
    ) == limits
    assert policy.max_tokens_per_request == limits[2]


def test_model_budget_override_keeps_the_other_groq_defaults():
    settings = ProviderSettings(
        _env_file=None, model_budgets={"groq/openai/gpt-oss-20b": {"max_requests_per_day": 5}}
    )
    assert settings.model_budgets["groq/openai/gpt-oss-20b"].max_requests_per_day == 5
    assert settings.model_budgets["groq/openai/gpt-oss-120b"].max_requests_per_day == 1000


@pytest.mark.parametrize("cap", ["max_requests_per_day", "max_tokens_per_day"])
def test_unpriced_providers_must_keep_daily_request_and_token_caps(cap):
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
