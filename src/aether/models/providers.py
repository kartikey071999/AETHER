"""Async SDK integrations. Construction does not contact a provider."""

from collections import deque
from typing import Any

import anthropic
import groq
import openai
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aether.models.base import ProviderError, RemoteProvider
from aether.models.contracts import (
    CompletionRequest,
    CompletionResponse,
    ModelInfo,
    ModelTier,
    ProviderHealth,
)
from aether.models.settings import ProviderSettings
from aether.models.transport import (
    HttpResponse,
    HttpTransport,
    create_anthropic_client,
    create_client,
)
from aether.security import PermissionService

_GENERAL = frozenset({ModelTier.LIGHT, ModelTier.STANDARD, ModelTier.REASONING, ModelTier.PREMIUM})
_REASONING = frozenset({ModelTier.STANDARD, ModelTier.REASONING, ModelTier.PREMIUM})
_LIGHT = frozenset({ModelTier.LIGHT, ModelTier.STANDARD})


def _response_format(request: CompletionRequest) -> dict[str, Any]:
    if request.json_schema is not None:
        return {
            "type": "json_schema",
            "json_schema": {"name": "completion", "strict": True, "schema": request.json_schema},
        }
    if request.response_format == "json_schema":
        raise ProviderError(request.provider or "unknown", code="json_schema_required")
    return {"type": request.response_format}


class _ChatMessage(BaseModel):
    content: str | None = None


class _ChatChoice(BaseModel):
    message: _ChatMessage
    finish_reason: str | None = None


class _ChatUsage(BaseModel):
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)


class _ChatResult(BaseModel):
    choices: list[_ChatChoice] = Field(min_length=1)
    usage: _ChatUsage | None = None


def _chat_response(request: CompletionRequest, provider: str, data: Any) -> CompletionResponse:
    try:
        result = _ChatResult.model_validate(data)
    except ValidationError:
        raise ProviderError(provider, code="invalid_response") from None
    return CompletionResponse(
        text=result.choices[0].message.content or "",
        model=request.model or "",
        provider=provider,
        input_tokens=(
            result.usage.prompt_tokens if result.usage else request.input_token_estimate()
        ),
        output_tokens=result.usage.completion_tokens if result.usage else request.max_tokens,
        usage_estimated=result.usage is None,
        latency_ms=0,
        finish_reason=result.choices[0].finish_reason,
    )


class GroqProvider(RemoteProvider):
    name = "groq"
    endpoint = "https://api.groq.com/openai/v1"

    def __init__(
        self,
        settings: ProviderSettings,
        permissions: PermissionService,
        *,
        client: groq.AsyncGroq | None = None,
    ) -> None:
        super().__init__(settings, permissions)
        self.client = client
        if client is None and self.available():
            self.client = groq.AsyncGroq(
                api_key=settings.groq_api_key.get_secret_value(),
                base_url=self.endpoint,
                max_retries=0,
                http_client=create_client(permissions, settings.timeout_seconds),
            )

    def available(self) -> bool:
        return self.settings.groq_api_key is not None

    async def list_models(self) -> list[ModelInfo]:
        if not self.available():
            return []
        return [
            ModelInfo(name="openai/gpt-oss-20b", tiers=_LIGHT),
            ModelInfo(name="openai/gpt-oss-120b", tiers=_REASONING),
            ModelInfo(name="qwen/qwen3.8-27b", tiers=_REASONING),
        ]

    async def _complete(self, request: CompletionRequest) -> CompletionResponse:
        if self.client is None:
            raise ProviderError(self.name, code="unavailable")
        options = {"temperature": request.temperature} if request.temperature is not None else {}
        result = await self.client.chat.completions.create(
            model=request.model,
            messages=[message.model_dump() for message in request.messages],
            max_tokens=request.max_tokens,
            response_format=_response_format(request),
            **options,
        )
        return _chat_response(request, self.name, result.model_dump())

    async def _probe(self) -> object:
        if self.client is None:
            raise ProviderError(self.name, code="unavailable")
        return await self.client.models.list()

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.close()


class AzureOpenAIProvider(RemoteProvider):
    name = "azure_openai"

    def __init__(
        self,
        settings: ProviderSettings,
        permissions: PermissionService,
        *,
        client: openai.AsyncAzureOpenAI | None = None,
    ) -> None:
        super().__init__(settings, permissions)
        self.endpoint = (
            settings.azure_openai_endpoint.get_secret_value()
            if settings.azure_openai_endpoint
            else ""
        )
        self.client = client
        if client is None and self.available():
            self.client = openai.AsyncAzureOpenAI(
                api_key=settings.azure_openai_key.get_secret_value(),
                azure_endpoint=self.endpoint,
                api_version=settings.openai_api_version.get_secret_value(),
                max_retries=0,
                http_client=create_client(permissions, settings.timeout_seconds),
            )

    def available(self) -> bool:
        return all(
            (
                self.settings.azure_openai_endpoint,
                self.settings.azure_openai_key,
                self.settings.openai_api_version,
            )
        )

    async def list_models(self) -> list[ModelInfo]:
        return (
            [ModelInfo(name="gpt-6-astra", tiers=frozenset({ModelTier.PREMIUM}))]
            if self.available()
            else []
        )

    async def _complete(self, request: CompletionRequest) -> CompletionResponse:
        if self.client is None:
            raise ProviderError(self.name, code="unavailable")
        options = {"temperature": request.temperature} if request.temperature is not None else {}
        result = await self.client.chat.completions.create(
            model=request.model,
            messages=[message.model_dump() for message in request.messages],
            max_completion_tokens=request.max_tokens,
            response_format=_response_format(request),
            **options,
        )
        return _chat_response(request, self.name, result.model_dump())

    async def _probe(self) -> object:
        if self.client is None:
            raise ProviderError(self.name, code="unavailable")
        return await self.client.models.list()

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.close()


class _AnthropicBlock(BaseModel):
    type: str
    text: str = ""


class _AnthropicUsage(BaseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class _AnthropicResult(BaseModel):
    content: list[_AnthropicBlock]
    usage: _AnthropicUsage
    stop_reason: str | None = None


class AzureAnthropicProvider(RemoteProvider):
    name = "azure_anthropic"

    def __init__(
        self,
        settings: ProviderSettings,
        permissions: PermissionService,
        *,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        super().__init__(settings, permissions)
        self.endpoint = (
            settings.anthropic_endpoint.get_secret_value() if settings.anthropic_endpoint else ""
        )
        self.client = client
        if client is None and self.available():
            self.client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key.get_secret_value(),
                base_url=self.endpoint,
                default_headers={
                    "anthropic-version": settings.anthropic_api_version.get_secret_value()
                },
                max_retries=0,
                http_client=create_anthropic_client(permissions, settings.timeout_seconds),
            )

    def available(self) -> bool:
        return all(
            (
                self.settings.anthropic_endpoint,
                self.settings.anthropic_api_key,
                self.settings.anthropic_api_version,
            )
        )

    async def list_models(self) -> list[ModelInfo]:
        return (
            [ModelInfo(name="claude-opus-4-8", tiers=frozenset({ModelTier.PREMIUM}))]
            if self.available()
            else []
        )

    async def _complete(self, request: CompletionRequest) -> CompletionResponse:
        if self.client is None:
            raise ProviderError(self.name, code="unavailable")
        if request.temperature is not None:
            raise ProviderError(self.name, code="temperature_unsupported")
        options: dict[str, Any] = {}
        if request.json_schema is not None:
            options["output_config"] = {
                "format": {"type": "json_schema", "schema": request.json_schema}
            }
        elif request.response_format != "text":
            raise ProviderError(self.name, code="json_schema_required")
        result = await self.client.messages.create(
            model=request.model,
            messages=[
                message.model_dump() for message in request.messages if message.role != "system"
            ],
            system="\n\n".join(
                message.content for message in request.messages if message.role == "system"
            ),
            max_tokens=request.max_tokens,
            **options,
        )
        try:
            data = _AnthropicResult.model_validate(result.model_dump())
        except ValidationError:
            raise ProviderError(self.name, code="invalid_response") from None
        return CompletionResponse(
            text="".join(block.text for block in data.content if block.type == "text"),
            model=request.model,
            provider=self.name,
            input_tokens=data.usage.input_tokens,
            output_tokens=data.usage.output_tokens,
            latency_ms=0,
            finish_reason=data.stop_reason,
        )

    async def _probe(self) -> object:
        if self.client is None:
            raise ProviderError(self.name, code="unavailable")
        return await self.client.models.list()

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.close()


class _OllamaResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: _ChatMessage
    prompt_eval_count: int | None = Field(default=None, ge=0)
    eval_count: int | None = Field(default=None, ge=0)
    done_reason: str | None = None


class _OllamaModel(BaseModel):
    name: str


class _OllamaTags(BaseModel):
    models: list[_OllamaModel]


class OllamaProvider(RemoteProvider):
    name = "ollama"

    def __init__(
        self,
        settings: ProviderSettings,
        permissions: PermissionService,
        *,
        transport: HttpTransport | None = None,
    ) -> None:
        super().__init__(settings, permissions)
        self.endpoint = settings.ollama_base_url.get_secret_value().rstrip("/")
        self.client = create_client(permissions, settings.timeout_seconds, transport=transport)
        self._models: list[ModelInfo] | None = None

    def available(self) -> bool:
        return bool(self.settings.ollama_models)

    async def refresh_models(self) -> list[ModelInfo]:
        if not self.available():
            return []
        response = await self._run(self._probe)
        try:
            tags = _OllamaTags.model_validate(response.json())
        except (ValueError, ValidationError):
            raise ProviderError(self.name, code="invalid_response") from None
        installed = {model.name for model in tags.models}
        self._models = [
            ModelInfo(name=name, tiers=frozenset(tiers))
            for name, tiers in self.settings.ollama_models.items()
            if name in installed
        ]
        return list(self._models)

    async def list_models(self) -> list[ModelInfo]:
        if self._models is None:
            return await self.refresh_models()
        return list(self._models)

    async def _probe(self) -> HttpResponse:
        response = await self.client.get(f"{self.endpoint}/api/tags")
        response.raise_for_status()
        return response

    async def _complete(self, request: CompletionRequest) -> CompletionResponse:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [message.model_dump() for message in request.messages],
            "stream": False,
            "options": {"num_predict": request.max_tokens},
        }
        if request.temperature is not None:
            payload["options"]["temperature"] = request.temperature
        if request.json_schema is not None:
            payload["format"] = request.json_schema
        elif request.response_format == "json_object":
            payload["format"] = "json"
        elif request.response_format == "json_schema":
            raise ProviderError(self.name, code="json_schema_required")
        response = await self.client.post(f"{self.endpoint}/api/chat", json=payload)
        response.raise_for_status()
        try:
            result = _OllamaResult.model_validate(response.json())
        except ValueError:
            raise ProviderError(self.name, code="invalid_response") from None
        return CompletionResponse(
            text=result.message.content or "",
            model=request.model,
            provider=self.name,
            input_tokens=(
                result.prompt_eval_count
                if result.prompt_eval_count is not None
                else request.input_token_estimate()
            ),
            output_tokens=(
                result.eval_count if result.eval_count is not None else request.max_tokens
            ),
            usage_estimated=result.prompt_eval_count is None or result.eval_count is None,
            latency_ms=0,
            finish_reason=result.done_reason,
        )

    async def aclose(self) -> None:
        await self.client.aclose()


class MockProvider:
    """No network, stable responses, and an optional FIFO of scripted outcomes."""

    def __init__(
        self,
        responses: list[str | CompletionResponse | Exception] | None = None,
        *,
        name: str = "mock",
        models: list[ModelInfo] | None = None,
        available: bool = True,
    ) -> None:
        self.name = name
        self._available = available
        self._models = (
            models if models is not None else [ModelInfo(name="mock-model", tiers=_GENERAL)]
        )
        self._responses = deque(responses or [])
        self.requests: list[CompletionRequest] = []

    def available(self) -> bool:
        return self._available

    async def list_models(self) -> list[ModelInfo]:
        return list(self._models) if self.available() else []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if request.tier is ModelTier.NONE:
            raise ValueError("deterministic work must not call an LLM")
        if not self.available():
            raise ProviderError(self.name, code="unavailable")
        self.requests.append(request)
        outcome = self._responses.popleft() if self._responses else "mock completion"
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, CompletionResponse):
            return outcome
        return CompletionResponse(
            text=outcome,
            provider=self.name,
            model=request.model or self._models[0].name,
            input_tokens=min(request.input_token_estimate(), 10),
            output_tokens=min(request.max_tokens, 5),
            latency_ms=0,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            available=self.available(), healthy=self.available(), detail="deterministic"
        )

    async def aclose(self) -> None:
        pass
