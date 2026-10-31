import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import httpx2
import pytest
from model_helpers import records
from sqlalchemy import select

from aether.models import (
    AzureAnthropicProvider,
    AzureOpenAIProvider,
    BudgetGuard,
    CompletionRequest,
    GroqProvider,
    MockProvider,
    ModelRouter,
    ModelTier,
    NoModelAvailable,
    OllamaProvider,
    ProviderSettings,
)
from aether.models import providers as integrations
from aether.models.costs import estimate_cost
from aether.models.transport import create_anthropic_client, create_client
from aether.observability.recorder import EventRecorder
from aether.security import OperationBlocked, PermissionService, SecurityPolicy
from aether.storage.models import EventRecord


def request(tier=ModelTier.LIGHT, **kwargs):
    return CompletionRequest(
        messages=[{"role": "user", "content": "Hello"}], max_tokens=5, tier=tier, **kwargs
    )


def configured(**kwargs):
    return ProviderSettings(
        _env_file=None,
        GROQ_API_KEY="test-groq-credential",
        AZURE_OPENAI_ENDPOINT="https://azure.example.com",
        AZURE_OPEN_AI_KEY="test-azure-credential",
        OPENAI_API_VERSION="2026-01-01",
        ANTHROPIC_ENDPOINT="https://opus.example.com/anthropic",
        ANTHROPIC_API_KEY="test-opus-credential",
        API_VERSION="2023-06-01",
        max_retries=0,
        **kwargs,
    )


def chat_response():
    return {
        "id": "completion-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
    }


@pytest.mark.parametrize(
    ("provider_type", "tier", "model"),
    [
        (GroqProvider, ModelTier.LIGHT, "openai/gpt-oss-20b"),
        (AzureOpenAIProvider, ModelTier.PREMIUM, "gpt-6-astra"),
        (AzureAnthropicProvider, ModelTier.PREMIUM, "claude-opus-4-8"),
    ],
)
async def test_real_sdk_payloads_and_results_with_mock_transport(
    permissions, usage, monkeypatch, provider_type, tier, model
):
    sent = []

    def handler(req):
        sent.append(req)
        if provider_type is AzureAnthropicProvider:
            data = {
                "id": "msg-test",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": "Hello"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 7, "output_tokens": 2},
            }
        else:
            data = chat_response()
        response_type = (
            httpx2.Response if provider_type is AzureAnthropicProvider else httpx.Response
        )
        return response_type(200, json=data)

    monkeypatch.setattr(
        integrations,
        "create_client",
        lambda permissions, timeout: create_client(
            permissions, timeout, transport=httpx.MockTransport(handler)
        ),
    )
    monkeypatch.setattr(
        integrations,
        "create_anthropic_client",
        lambda permissions, timeout: create_anthropic_client(
            permissions, timeout, transport=httpx2.MockTransport(handler)
        ),
    )
    settings = configured()
    provider = provider_type(settings, permissions)
    try:
        result = await ModelRouter([provider], BudgetGuard(usage, settings)).complete(request(tier))
    finally:
        await provider.aclose()
    assert result.text == "Hello" and result.model == model
    assert result.input_tokens == 7 and result.output_tokens == 2
    assert result.finish_reason in {"stop", "end_turn"}
    assert len(sent) == 1
    payload = json.loads(sent[0].content)
    assert payload["model"] == model
    assert payload["messages"] == [{"role": "user", "content": "Hello"}]
    key = "max_completion_tokens" if provider_type is AzureOpenAIProvider else "max_tokens"
    assert payload[key] == 5
    if provider_type is AzureAnthropicProvider:
        assert sent[0].headers["anthropic-version"] == "2023-06-01"
    elif provider_type is AzureOpenAIProvider:
        assert sent[0].url.params["api-version"] == "2026-01-01"
    assert "temperature" not in payload


@pytest.mark.parametrize(
    "provider_type", [GroqProvider, AzureOpenAIProvider, AzureAnthropicProvider]
)
async def test_json_schema_mapping(permissions, monkeypatch, provider_type):
    sent = []
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}

    def handler(req):
        sent.append(req)
        if provider_type is AzureAnthropicProvider:
            return httpx2.Response(
                200,
                json={
                    "id": "msg-test",
                    "type": "message",
                    "role": "assistant",
                    "model": "test",
                    "content": [{"type": "text", "text": '{"answer":"ok"}'}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 7, "output_tokens": 2},
                },
            )
        return httpx.Response(200, json=chat_response())

    monkeypatch.setattr(
        integrations,
        "create_client",
        lambda permissions, timeout: create_client(
            permissions, timeout, transport=httpx.MockTransport(handler)
        ),
    )
    monkeypatch.setattr(
        integrations,
        "create_anthropic_client",
        lambda permissions, timeout: create_anthropic_client(
            permissions, timeout, transport=httpx2.MockTransport(handler)
        ),
    )
    provider = provider_type(configured(), permissions)
    model = (await provider.list_models())[0].name
    tier = ModelTier.LIGHT if provider_type is GroqProvider else ModelTier.PREMIUM
    try:
        await provider.complete(request(tier, model=model, json_schema=schema))
    finally:
        await provider.aclose()
    payload = json.loads(sent[0].content)
    if provider_type is AzureAnthropicProvider:
        assert payload["output_config"]["format"]["schema"] == schema
    else:
        assert payload["response_format"]["json_schema"]["schema"] == schema


async def test_anthropic_temperature_request_is_explicitly_rejected(permissions):
    from aether.models import ProviderError

    create = AsyncMock()
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    provider = AzureAnthropicProvider(configured(), permissions, client=client)
    with pytest.raises(ProviderError, match="temperature_unsupported"):
        await provider.complete(
            request(
                ModelTier.PREMIUM,
                model="claude-opus-4-8",
                temperature=0,
            )
        )
    create.assert_not_awaited()


async def test_unconfigured_providers_are_unavailable(settings, permissions):
    providers = [
        GroqProvider(settings, permissions),
        AzureOpenAIProvider(settings, permissions),
        AzureAnthropicProvider(settings, permissions),
        OllamaProvider(settings, permissions),
    ]
    for provider in providers:
        assert not provider.available()
        assert await provider.list_models() == []
        assert not (await provider.health()).healthy
        await provider.aclose()


async def test_policy_blocked_host_makes_no_sdk_call(database, tmp_path):
    policy = SecurityPolicy(
        workspace_dir=tmp_path, artifact_dir=tmp_path, data_dir=tmp_path, allowed_hosts=[]
    )
    permissions = PermissionService(database.sessions, EventRecorder(), policy)
    create = AsyncMock()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    settings = configured()
    provider = GroqProvider(settings, permissions, client=client)
    with pytest.raises(OperationBlocked):
        await provider.complete(request(model="openai/gpt-oss-20b"))
    create.assert_not_awaited()


async def test_policy_blocked_provider_usage_is_persisted(database, tmp_path, usage, monkeypatch):
    sent = []
    policy = SecurityPolicy(workspace_dir=tmp_path, artifact_dir=tmp_path, data_dir=tmp_path)
    permissions = PermissionService(database.sessions, EventRecorder(), policy)
    monkeypatch.setattr(
        integrations,
        "create_client",
        lambda permissions, timeout: create_client(
            permissions,
            timeout,
            transport=httpx.MockTransport(lambda req: sent.append(req) or httpx.Response(200)),
        ),
    )
    settings = configured()
    provider = GroqProvider(settings, permissions)
    try:
        with pytest.raises(NoModelAvailable):
            await ModelRouter([provider], BudgetGuard(usage, settings)).complete(request())
    finally:
        await provider.aclose()
    assert not sent
    rows, events = await records(database)
    assert len(rows) == len(events) == 1
    assert rows[0].status == events[0].payload["status"] == "blocked"
    assert rows[0].input_tokens == rows[0].output_tokens == 0


async def test_transport_requires_context_and_rechecks_actual_host(permissions):
    sent = []
    async with create_client(
        permissions,
        1,
        transport=httpx.MockTransport(lambda req: sent.append(req) or httpx.Response(200)),
    ) as client:
        with pytest.raises(RuntimeError, match="context"):
            await client.get("https://api.groq.com/test")
    assert not sent


async def test_sdk_endpoint_mismatch_is_blocked_before_transport(permissions, usage, monkeypatch):
    import groq

    sent = []
    settings = configured()
    client = groq.AsyncGroq(
        api_key="test",
        base_url="https://not-allowlisted.example.com",
        max_retries=0,
        http_client=create_client(
            permissions,
            1,
            transport=httpx.MockTransport(lambda req: sent.append(req) or httpx.Response(200)),
        ),
    )
    provider = GroqProvider(settings, permissions, client=client)
    try:
        with pytest.raises(NoModelAvailable):
            await ModelRouter([provider], BudgetGuard(usage, settings)).complete(request())
    finally:
        await provider.aclose()
    assert not sent


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_sdk_transient_errors_retry_without_implicit_extra_calls(
    permissions, usage, database, monkeypatch, status
):
    sent = []
    sleeps = []

    def handler(req):
        sent.append(req)
        return httpx.Response(status, json={"error": {"message": "test failure"}})

    async def sleeper(delay):
        sleeps.append(delay)

    monkeypatch.setattr(
        integrations,
        "create_client",
        lambda permissions, timeout: create_client(
            permissions, timeout, transport=httpx.MockTransport(handler)
        ),
    )
    settings = configured().model_copy(update={"max_retries": 2})
    provider = GroqProvider(settings, permissions)
    try:
        with pytest.raises(NoModelAvailable):
            await ModelRouter([provider], BudgetGuard(usage, settings), sleeper=sleeper).complete(
                request()
            )
    finally:
        await provider.aclose()
    assert len(sent) == 3 and sleeps == [0.5, 1.0]
    rows, events = await records(database)
    assert len(rows) == len(events) == 3
    assert all(event.payload["error_type"] == "ProviderError" for event in events)


async def test_nontransient_errors_do_not_retry(permissions, usage, monkeypatch):
    sent = []

    def handler(req):
        sent.append(req)
        return httpx.Response(401, json={"error": {"message": "invalid credential"}})

    monkeypatch.setattr(
        integrations,
        "create_client",
        lambda permissions, timeout: create_client(
            permissions, timeout, transport=httpx.MockTransport(handler)
        ),
    )
    settings = configured().model_copy(update={"max_retries": 2})
    provider = GroqProvider(settings, permissions)
    try:
        with pytest.raises(NoModelAvailable):
            await ModelRouter([provider], BudgetGuard(usage, settings)).complete(request())
    finally:
        await provider.aclose()
    assert len(sent) == 1


async def test_ollama_installed_suitable_model_is_local_first(permissions, usage):
    sent = []

    def handler(req):
        sent.append(req)
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "local-small:latest"}]})
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "local reply"},
                "prompt_eval_count": 4,
                "eval_count": 2,
                "done_reason": "stop",
            },
        )

    settings = ProviderSettings(
        _env_file=None,
        ollama_models={
            "local-small:latest": [ModelTier.LIGHT],
            "absent:latest": [ModelTier.REASONING],
        },
    )
    local = OllamaProvider(settings, permissions, transport=httpx.MockTransport(handler))
    remote = MockProvider(name="groq")
    try:
        result = await ModelRouter([remote, local], BudgetGuard(usage, settings)).complete(
            request()
        )
    finally:
        await local.aclose()
    assert result.provider == "ollama" and not remote.requests
    assert result.text == "local reply" and result.cost_usd == 0
    assert [req.url.path for req in sent] == ["/api/tags", "/api/chat"]
    payload = json.loads(sent[1].content)
    assert payload["stream"] is False and payload["options"]["num_predict"] == 5


async def test_ollama_uninstalled_models_fall_back(permissions, usage):
    settings = ProviderSettings(_env_file=None, ollama_models={"absent": [ModelTier.LIGHT]})
    local = OllamaProvider(
        settings,
        permissions,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"models": []})),
    )
    try:
        result = await ModelRouter([local, MockProvider()], BudgetGuard(usage, settings)).complete(
            request()
        )
    finally:
        await local.aclose()
    assert result.provider == "mock"


async def test_secrets_and_prompts_absent_from_logs_and_events(
    permissions, usage, database, monkeypatch, caplog
):
    import logging

    caplog.set_level(logging.DEBUG)
    secret = "sensitive-arbitrary-string"
    error_secret = "provider-error-private-string"

    def handler(req):
        return httpx.Response(401, json={"error": {"message": error_secret}})

    monkeypatch.setattr(
        integrations,
        "create_client",
        lambda permissions, timeout: create_client(
            permissions, timeout, transport=httpx.MockTransport(handler)
        ),
    )
    settings = configured()
    provider = GroqProvider(settings, permissions)
    private_request = request().model_copy(
        update={"messages": (request().messages[0].model_copy(update={"content": secret}),)}
    )
    try:
        with pytest.raises(NoModelAvailable) as captured:
            await ModelRouter([provider], BudgetGuard(usage, settings)).complete(private_request)
    finally:
        await provider.aclose()
    async with database.sessions() as session:
        events = list(await session.scalars(select(EventRecord)))
    content = repr([event.payload for event in events]) + caplog.text + str(captured.value)
    assert secret not in content and error_secret not in content
    assert "test-groq-credential" not in content
    assert secret not in repr(private_request)


@pytest.mark.parametrize(
    ("provider", "model", "expected", "unknown"),
    [
        ("groq", "openai/gpt-oss-20b", 0.375, False),
        ("groq", "openai/gpt-oss-120b", 0.75, False),
        ("groq", "qwen/qwen3.8-27b", 4.8, False),
        ("azure_openai", "gpt-6-astra", 0, True),
        ("ollama", "local-model", 0, False),
    ],
)
def test_cost_table(provider, model, expected, unknown):
    cost, flag = estimate_cost(provider, model, 1_000_000, 1_000_000)
    assert float(cost) == pytest.approx(expected) and flag is unknown


def test_groq_base_url_is_not_doubled() -> None:
    # The groq SDK appends /openai/v1/... itself; a base URL ending in /openai/v1 yields HTTP 404.
    from aether.models.providers import GroqProvider

    assert GroqProvider.endpoint == "https://api.groq.com"
