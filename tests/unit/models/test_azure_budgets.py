"""Azure output budgets, reasoning effort, WARDEN's llm_call permission and error details."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest
from model_helpers import records

from aether.communication.messages import AgentId
from aether.models import (
    AzureAnthropicProvider,
    AzureOpenAIProvider,
    BudgetGuard,
    CompletionRequest,
    ModelRouter,
    ModelTier,
    ProviderSettings,
)
from aether.models import providers as integrations


def configured() -> ProviderSettings:
    return ProviderSettings(
        _env_file=None,
        AZURE_OPENAI_ENDPOINT="https://azure.example.com",
        AZURE_OPEN_AI_KEY="test-azure-credential",
        OPENAI_API_VERSION="2026-01-01",
        ANTHROPIC_ENDPOINT="https://opus.example.com/anthropic",
        ANTHROPIC_API_KEY="test-opus-credential",
        API_VERSION="2023-06-01",
        max_retries=0,
    )


def scout_request(**overrides) -> CompletionRequest:
    # The research-summary shape SCOUT and ROGUE send.
    return CompletionRequest(
        **{
            "messages": [{"role": "user", "content": "Summarise the evidence."}],
            "tier": ModelTier.LIGHT,
            "agent": AgentId.SCOUT,
            "response_format": "json_schema",
            "json_schema": {"type": "object", "properties": {}, "required": []},
            "max_tokens": 2048,
            "max_tokens_cap": 1024,
            **overrides,
        }
    )


def chat_result(text: str, finish: str = "stop", output: int = 10):
    data = {
        "choices": [{"message": {"content": text}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 20, "completion_tokens": output},
    }
    return SimpleNamespace(model_dump=lambda: data)


def anthropic_result(text: str, stop: str = "end_turn", output: int = 10):
    data = {
        "content": [{"type": "text", "text": text}] if text else [],
        "usage": {"input_tokens": 20, "output_tokens": output},
        "stop_reason": stop,
    }
    return SimpleNamespace(model_dump=lambda: data)


def astra(permissions, create):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return AzureOpenAIProvider(configured(), permissions, client=client)


def opus(permissions, create):
    return AzureAnthropicProvider(
        configured(), permissions, client=SimpleNamespace(messages=SimpleNamespace(create=create))
    )


def router(provider, usage):
    return ModelRouter([provider], BudgetGuard(usage, configured()))


async def test_astra_gets_a_reasoning_sized_budget_with_low_effort(permissions, usage):
    create = AsyncMock(return_value=chat_result("{}"))
    result = await router(astra(permissions, create), usage).complete(scout_request())
    assert (result.provider, result.model) == ("azure_openai", "gpt-6-astra")
    kwargs = create.await_args.kwargs
    assert kwargs["max_completion_tokens"] == integrations.ASTRA_MIN_MAX_TOKENS >= 4096
    assert kwargs["reasoning_effort"] == "low"


async def test_opus_gets_a_larger_budget_despite_a_small_request_cap(permissions, usage):
    create = AsyncMock(return_value=anthropic_result("{}"))
    result = await router(opus(permissions, create), usage).complete(
        scout_request(max_tokens=1024, max_tokens_cap=1024)
    )
    assert (result.provider, result.model) == ("azure_anthropic", "claude-opus-4-8")
    assert create.await_args.kwargs["max_tokens"] == integrations.OPUS_MIN_MAX_TOKENS >= 2048


async def test_request_cap_is_raised_to_the_model_minimum_not_failed(permissions, usage):
    create = AsyncMock(return_value=chat_result("{}"))
    await router(astra(permissions, create), usage).complete(
        scout_request(max_tokens=512, max_tokens_cap=512)
    )
    assert create.await_count == 1


async def test_reasoning_effort_is_dropped_when_the_deployment_rejects_it(permissions, usage):
    rejected = openai.BadRequestError(
        "Unsupported parameter: reasoning_effort",
        response=httpx.Response(400, request=httpx.Request("POST", "https://azure.example.com")),
        body=None,
    )
    create = AsyncMock(side_effect=[rejected, chat_result("{}")])
    result = await router(astra(permissions, create), usage).complete(scout_request())
    assert result.text == "{}"
    first, second = (call.kwargs for call in create.await_args_list)
    assert first["reasoning_effort"] == "low" and "reasoning_effort" not in second
    assert second["max_completion_tokens"] == first["max_completion_tokens"]


async def test_other_bad_requests_are_not_retried_without_effort(permissions, usage):
    rejected = openai.BadRequestError(
        "invalid thing",
        response=httpx.Response(400, request=httpx.Request("POST", "https://azure.example.com")),
        body=None,
    )
    create = AsyncMock(side_effect=rejected)
    provider = astra(permissions, create)
    with pytest.raises(Exception):  # noqa: B017 - SDK/router error type is irrelevant here
        await router(provider, usage).complete(
            scout_request(json_schema=None, response_format="text")
        )
    assert create.await_count == 1


@pytest.mark.parametrize(
    ("builder", "result", "expected"),
    [
        (
            astra,
            chat_result("", finish="length", output=4096),
            "finish_reason=length, output_tokens=4096, max_tokens=4096",
        ),
        (
            astra,
            chat_result("", finish="stop", output=0),
            "finish_reason=stop, output_tokens=0, max_tokens=4096",
        ),
        (
            opus,
            anthropic_result('{"a":', stop="max_tokens", output=2048),
            "finish_reason=max_tokens, output_tokens=2048, max_tokens=2048",
        ),
    ],
)
async def test_empty_and_truncated_completions_record_a_diagnostic_error_detail(
    permissions, usage, database, builder, result, expected
):
    create = AsyncMock(return_value=result)
    with pytest.raises(Exception):  # noqa: B017 - NoModelAvailable wraps the failure
        await router(builder(permissions, create), usage).complete(scout_request())
    _, events = await records(database)
    completed = events
    assert completed and completed[-1].payload["status"] == "failed"
    assert completed[-1].payload["error_detail"] == expected
    assert "{" not in completed[-1].payload["error_detail"]


async def test_warden_azure_call_is_authorized_and_recorded(permissions, usage, database):
    create = AsyncMock(return_value=chat_result('{"summary": "ok"}'))
    result = await router(astra(permissions, create), usage).complete(
        scout_request(agent=AgentId.WARDEN, tier=ModelTier.REASONING, max_tokens=2048)
    )
    assert result.provider == "azure_openai"
    rows, _ = await records(database)
    assert [(row.agent, row.status) for row in rows] == [("WARDEN", "succeeded")]
