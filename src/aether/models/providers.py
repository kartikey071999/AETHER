"""Async SDK integrations. Construction does not contact a provider."""

import json
from collections import deque
from typing import Any

import anthropic
import groq
import openai
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aether.models.base import EmptyCompletion, ProviderError, RemoteProvider
from aether.models.contracts import (
    CompletionRequest,
    CompletionResponse,
    ModelInfo,
    ModelTier,
    ProviderHealth,
)
from aether.models.schemas import Flavour, strict_json_schema
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

# Output budgets that fit real answers: the reasoning deployment spends part of it on hidden
# reasoning, and the structured research JSON is longer than a 1k budget allows.
ASTRA_MODEL = "gpt-6-astra"
ASTRA_MIN_MAX_TOKENS = 4096
OPUS_MODEL = "claude-opus-4-8"
OPUS_MIN_MAX_TOKENS = 2048


def _response_format(
    request: CompletionRequest, *, flavour: Flavour | None = None
) -> dict[str, Any]:
    if request.json_schema is not None:
        schema = (
            strict_json_schema(request.json_schema, flavour=flavour)
            if flavour is not None
            else request.json_schema
        )
        return {
            "type": "json_schema",
            "json_schema": {"name": "completion", "strict": True, "schema": schema},
        }
    if request.response_format == "json_schema":
        raise ProviderError(request.provider or "unknown", code="json_schema_required")
    return {"type": request.response_format}


def _schema_instruction(request: CompletionRequest) -> str:
    return (
        "Respond with a single JSON object and nothing else (no prose, no code fences) "
        "that conforms to this JSON schema:\n"
        + json.dumps(request.json_schema, ensure_ascii=False, separators=(",", ":"))
    )


def _json_text(provider: str, text: str) -> str:
    """The completion's JSON object, tolerating a code fence; anything else is unusable."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.removeprefix("```json").removeprefix("```").removesuffix("```")
        candidate = candidate.strip()
    try:
        parsed = json.loads(candidate)
    except ValueError:
        raise ProviderError(provider, code="invalid_json") from None
    if not isinstance(parsed, dict):
        raise ProviderError(provider, code="invalid_json")
    return candidate


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
    endpoint = "https://api.groq.com"  # the SDK appends /openai/v1/... itself

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
            ModelInfo(name="openai/gpt-oss-20b", tiers=_LIGHT, min_max_tokens=128),
            ModelInfo(name="openai/gpt-oss-120b", tiers=_REASONING, min_max_tokens=128),
            ModelInfo(name="qwen/qwen3.8-27b", tiers=_REASONING, min_max_tokens=128),
        ]

    async def _complete(self, request: CompletionRequest) -> CompletionResponse:
        if self.client is None:
            raise ProviderError(self.name, code="unavailable")
        options: dict[str, float | str] = (
            {"temperature": request.temperature} if request.temperature is not None else {}
        )
        if request.model is not None and request.model.startswith("openai/gpt-oss-"):
            options["reasoning_effort"] = "low"
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
            [
                # Every tier: which agents and tiers use it is decided by the model assignment.
                # A reasoning deployment: its hidden reasoning shares the output budget.
                ModelInfo(name=ASTRA_MODEL, tiers=_GENERAL, min_max_tokens=ASTRA_MIN_MAX_TOKENS)
            ]
            if self.available()
            else []
        )

    async def _create(
        self, request: CompletionRequest, messages: list[dict[str, str]], **kwargs: Any
    ) -> Any:
        """One chat call; low reasoning effort is dropped if the deployment rejects it."""
        if self.client is None:
            raise ProviderError(self.name, code="unavailable")
        options: dict[str, Any] = (
            {"temperature": request.temperature} if request.temperature is not None else {}
        )
        if request.model == ASTRA_MODEL:
            options["reasoning_effort"] = "low"
        try:
            return await self.client.chat.completions.create(
                model=request.model,
                messages=messages,
                max_completion_tokens=request.max_tokens,
                **options,
                **kwargs,
            )
        except openai.BadRequestError as error:
            if "reasoning_effort" not in options or "reasoning_effort" not in str(error):
                raise
            del options["reasoning_effort"]
            return await self.client.chat.completions.create(
                model=request.model,
                messages=messages,
                max_completion_tokens=request.max_tokens,
                **options,
                **kwargs,
            )

    async def _complete(self, request: CompletionRequest) -> CompletionResponse:
        if self.client is None:
            raise ProviderError(self.name, code="unavailable")
        messages = [message.model_dump() for message in request.messages]
        try:
            result = await self._create(
                request, messages, response_format=_response_format(request, flavour="openai")
            )
        except openai.BadRequestError:
            if request.json_schema is None:
                raise
            # The schema was rejected: one retry in plain JSON mode; the caller validates locally.
            result = await self._create(
                request,
                [{"role": "system", "content": _schema_instruction(request)}, *messages],
                response_format={"type": "json_object"},
            )
            response = _chat_response(request, self.name, result.model_dump())
            if not response.text.strip():
                return response
            return response.model_copy(update={"text": _json_text(self.name, response.text)})
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
            [ModelInfo(name=OPUS_MODEL, tiers=_GENERAL, min_max_tokens=OPUS_MIN_MAX_TOKENS)]
            if self.available()
            else []
        )

    async def _complete(self, request: CompletionRequest) -> CompletionResponse:
        if self.client is None:
            raise ProviderError(self.name, code="unavailable")
        if request.temperature is not None:
            raise ProviderError(self.name, code="temperature_unsupported")
        options: dict[str, Any] = {}
        system = "\n\n".join(
            message.content for message in request.messages if message.role == "system"
        )
        if request.json_schema is not None:
            options["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": strict_json_schema(request.json_schema, flavour="anthropic"),
                }
            }
        elif request.response_format != "text":
            raise ProviderError(self.name, code="json_schema_required")
        messages = [
            message.model_dump() for message in request.messages if message.role != "system"
        ]
        try:
            result = await self.client.messages.create(
                model=request.model,
                messages=messages,
                system=system,
                max_tokens=request.max_tokens,
                **options,
            )
        except anthropic.BadRequestError:
            if request.json_schema is None:
                raise
            # The schema was rejected: one retry with prompt-level JSON instructions.
            result = await self.client.messages.create(
                model=request.model,
                messages=messages,
                system=f"{system}\n\n{_schema_instruction(request)}".strip(),
                max_tokens=request.max_tokens,
            )
            fallback = True
        else:
            fallback = False
        try:
            data = _AnthropicResult.model_validate(result.model_dump())
        except ValidationError:
            raise ProviderError(self.name, code="invalid_response") from None
        text = "".join(block.text for block in data.content if block.type == "text")
        return CompletionResponse(
            text=_json_text(self.name, text) if fallback and text.strip() else text,
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
        model = next((model for model in self._models if model.name == request.model), None)
        if model is not None:
            try:
                request = request.with_model_limits(model)
            except ValueError as error:
                raise ProviderError(self.name, code=str(error)) from None
        self.requests.append(request)
        outcome = self._responses.popleft() if self._responses else "mock completion"
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, CompletionResponse):
            response = outcome
        else:
            response = CompletionResponse(
                text=outcome,
                provider=self.name,
                model=request.model or self._models[0].name,
                input_tokens=min(request.input_token_estimate(), 10),
                output_tokens=min(request.max_tokens, 5),
                latency_ms=0,
            )
        if not response.text.strip():
            raise EmptyCompletion(response, max_tokens=request.max_tokens)
        return response

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            available=self.available(), healthy=self.available(), detail="deterministic"
        )

    async def aclose(self) -> None:
        pass
