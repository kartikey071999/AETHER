import json
from copy import deepcopy

import httpx
import httpx2
import pytest
from model_helpers import records
from pydantic import BaseModel
from test_providers import chat_response, configured

from aether.agents.orchestr.schemas import PlanRefinement
from aether.agents.rogue.schemas import CritiqueDraft, DivergentDraft
from aether.agents.scout.schemas import FindingDraft
from aether.models import (
    AzureAnthropicProvider,
    AzureOpenAIProvider,
    BudgetGuard,
    CompletionRequest,
    ModelRouter,
    ModelTier,
    NoModelAvailable,
    strict_json_schema,
)
from aether.models import providers as integrations
from aether.models.base import ERROR_DETAIL_LIMIT
from aether.models.transport import create_anthropic_client, create_client

AGENT_SCHEMAS = [FindingDraft, DivergentDraft, CritiqueDraft, PlanRefinement]
UNSUPPORTED = {
    "default",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "maxItems",
    "oneOf",
    "$ref",
    "$defs",
}


def objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            yield node
        for value in node.values():
            yield from objects(value)
    elif isinstance(node, list):
        for item in node:
            yield from objects(item)


def keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            if key != "properties":
                yield from keys(value)
            else:
                for sub in value.values():
                    yield from keys(sub)
    elif isinstance(node, list):
        for item in node:
            yield from keys(item)


@pytest.mark.parametrize("model", AGENT_SCHEMAS, ids=lambda model: model.__name__)
@pytest.mark.parametrize("flavour", ["openai", "anthropic", "generic"])
def test_every_agent_schema_is_strict(model: type[BaseModel], flavour):
    original = model.model_json_schema()
    snapshot = deepcopy(original)
    schema = strict_json_schema(original, flavour=flavour)
    assert original == snapshot
    found = list(objects(schema))
    assert found and all(node["additionalProperties"] is False for node in found)
    assert "$ref" not in json.dumps(schema) and "$defs" not in schema
    if flavour != "generic":
        assert not UNSUPPORTED & set(keys(schema))
    if flavour == "openai":
        for node in found:
            assert node["required"] == list(node["properties"])


def test_openai_optional_properties_become_nullable_and_required():
    schema = strict_json_schema(FindingDraft.model_json_schema(), flavour="openai")
    properties = schema["properties"]
    assert set(schema["required"]) == set(properties)
    assert properties["next_steps"]["anyOf"][-1] == {"type": "null"}
    assert properties["summary"]["type"] == "string"
    candidate = properties["candidates"]["items"]
    assert candidate["required"] == list(candidate["properties"])
    nested = candidate["properties"]["resource_needs"]["anyOf"]
    assert sum(item == {"type": "null"} for item in nested) == 1
    assert nested[0]["additionalProperties"] is False
    assert schema["properties"]["classification"]["anyOf"][0]["enum"]


def test_anthropic_keeps_required_and_generic_keeps_constraints():
    anthropic_schema = strict_json_schema(FindingDraft.model_json_schema(), flavour="anthropic")
    assert anthropic_schema["required"] == ["summary", "candidates", "recommendation", "confidence"]
    generic = strict_json_schema(FindingDraft.model_json_schema(), flavour="generic")
    assert generic["properties"]["summary"]["minLength"] == 1
    assert generic["properties"]["candidates"]["maxItems"] == 8


def test_dropped_constraints_are_restated_in_descriptions():
    schema = strict_json_schema(CritiqueDraft.model_json_schema(), flavour="openai")
    ranked = schema["properties"]["ranked_indices"]
    assert ranked["description"] == "Must contain at least 1 item. At most 8 items."
    assert "minItems" not in ranked and "maxItems" not in ranked
    assert schema["properties"]["rationale"]["description"] == "At least 1 character."

    finding = strict_json_schema(FindingDraft.model_json_schema(), flavour="openai")
    assert finding["properties"]["confidence"]["description"] == "Between 0 and 1."
    assert finding["properties"]["candidates"]["description"] == (
        "Must contain at least 1 item. At most 8 items."
    )
    resources = finding["properties"]["candidates"]["items"]["properties"]["resource_needs"]
    cpu = resources["anyOf"][0]["properties"]["cpu_percent"]["anyOf"][0]
    assert cpu["description"] == "Between 0.0 and 100.0."


def test_descriptions_keep_existing_text_and_anthropic_keeps_one_item_minimum():
    source = {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string", "maxLength": 20},
                "description": "Topic tags.",
                "minItems": 1,
                "maxItems": 3,
                "uniqueItems": True,
            },
            "mood": {"type": "string", "format": "emoji"},
            "score": {"type": "integer", "exclusiveMinimum": 0, "multipleOf": 5},
        },
        "required": ["tags", "mood", "score"],
    }
    openai_schema = strict_json_schema(source, flavour="openai")["properties"]
    assert openai_schema["tags"]["description"] == (
        "Topic tags. Must contain at least 1 item. At most 3 items. Items must be unique."
    )
    assert openai_schema["tags"]["items"]["description"] == "At most 20 characters."
    assert (
        openai_schema["mood"]["description"] == "Format: emoji."
        and "format" not in openai_schema["mood"]
    )
    assert openai_schema["score"]["description"] == "Greater than 0. Must be a multiple of 5."
    anthropic_tags = strict_json_schema(source, flavour="anthropic")["properties"]["tags"]
    assert anthropic_tags["minItems"] == 1
    assert anthropic_tags["description"] == "Topic tags. At most 3 items. Items must be unique."
    generic_tags = strict_json_schema(source, flavour="generic")["properties"]["tags"]
    assert generic_tags["description"] == "Topic tags." and generic_tags["maxItems"] == 3


def test_recursive_refs_are_kept_and_normalised():
    schema = {
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {
                    "child": {"anyOf": [{"$ref": "#/$defs/Node"}, {"type": "null"}]},
                    "label": {"type": "string"},
                },
                "required": ["label"],
            }
        },
        "type": "object",
        "properties": {"root": {"$ref": "#/$defs/Node"}},
        "required": ["root"],
    }
    result = strict_json_schema(schema, flavour="openai")
    node = result["$defs"]["Node"]
    assert node["additionalProperties"] is False and node["required"] == ["child", "label"]
    assert result["properties"]["root"]["properties"]["child"]["anyOf"][0] == {
        "$ref": "#/$defs/Node"
    }


def test_invalid_input_is_rejected():
    with pytest.raises(ValueError, match="unresolvable"):
        strict_json_schema(
            {"type": "object", "properties": {"a": {"$ref": "#/$defs/X"}}}, flavour="openai"
        )
    with pytest.raises(ValueError, match="flavour"):
        strict_json_schema({"type": "object"}, flavour="other")  # type: ignore[arg-type]


def anthropic_body(text):
    return {
        "id": "msg-test",
        "type": "message",
        "role": "assistant",
        "model": "test",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 7, "output_tokens": 2},
    }


def install(monkeypatch, handler):
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


def structured_request():
    return CompletionRequest(
        messages=[
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Plan it."},
        ],
        max_tokens=200,
        tier=ModelTier.PREMIUM,
        response_format="json_schema",
        json_schema=PlanRefinement.model_json_schema(),
    )


def rejected(response_type, message="schema is not supported"):
    return response_type(400, json={"error": {"message": message, "type": "invalid_request_error"}})


async def test_azure_openai_retries_once_with_json_object(permissions, usage, monkeypatch):
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        if len(sent) == 1:
            return rejected(httpx.Response)
        body = chat_response()
        body["choices"][0]["message"]["content"] = '```json\n{"extra_tasks": []}\n```'
        return httpx.Response(200, json=body)

    install(monkeypatch, handler)
    settings = configured()
    provider = AzureOpenAIProvider(settings, permissions)
    try:
        result = await ModelRouter([provider], BudgetGuard(usage, settings)).complete(
            structured_request()
        )
    finally:
        await provider.aclose()
    assert len(sent) == 2
    assert sent[0]["response_format"]["type"] == "json_schema"
    assert sent[1]["response_format"] == {"type": "json_object"}
    assert sent[1]["messages"][0]["role"] == "system"
    assert "JSON schema" in sent[1]["messages"][0]["content"]
    assert PlanRefinement.model_validate_json(result.text).extra_tasks == []


async def test_azure_anthropic_retries_once_with_prompt_instructions(
    permissions, usage, monkeypatch
):
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        if len(sent) == 1:
            return rejected(httpx2.Response)
        return httpx2.Response(200, json=anthropic_body('{"notes": "ok"}'))

    install(monkeypatch, handler)
    settings = configured()
    provider = AzureAnthropicProvider(settings, permissions)
    try:
        result = await ModelRouter([provider], BudgetGuard(usage, settings)).complete(
            structured_request()
        )
    finally:
        await provider.aclose()
    assert len(sent) == 2
    assert "output_config" in sent[0] and "output_config" not in sent[1]
    assert sent[1]["system"].startswith("Be brief.") and "JSON schema" in sent[1]["system"]
    assert PlanRefinement.model_validate_json(result.text).notes == "ok"


@pytest.mark.parametrize("provider_type", [AzureOpenAIProvider, AzureAnthropicProvider])
async def test_fallback_never_loops_and_failure_detail_is_recorded(
    permissions, usage, database, monkeypatch, provider_type
):
    sent = []
    secret = "sk-" + "a" * 30
    response_type = httpx2.Response if provider_type is AzureAnthropicProvider else httpx.Response

    def handler(req):
        sent.append(req)
        return rejected(response_type, "bad schema " + "x" * 400 + f" api_key={secret}")

    install(monkeypatch, handler)
    settings = configured()
    provider = provider_type(settings, permissions)
    try:
        with pytest.raises(NoModelAvailable):
            await ModelRouter([provider], BudgetGuard(usage, settings)).complete(
                structured_request()
            )
    finally:
        await provider.aclose()
    assert len(sent) == 2
    _, events = await records(database)
    detail = events[0].payload["error_detail"]
    assert "bad schema" in detail and detail.startswith("HTTP 400:")
    assert len(detail) <= ERROR_DETAIL_LIMIT
    assert secret not in detail and secret not in json.dumps(events[0].payload)


async def test_detail_redacts_secrets_before_truncating(permissions, usage, database, monkeypatch):
    secret = "sk-" + "b" * 40

    def handler(req):
        return httpx.Response(401, json={"error": {"message": f"key {secret}"}})

    install(monkeypatch, handler)
    settings = configured()
    provider = AzureOpenAIProvider(settings, permissions)
    try:
        with pytest.raises(NoModelAvailable):
            await ModelRouter([provider], BudgetGuard(usage, settings)).complete(
                CompletionRequest(
                    messages=[{"role": "user", "content": "hi"}], tier=ModelTier.PREMIUM
                )
            )
    finally:
        await provider.aclose()
    _, events = await records(database)
    assert events[0].payload["error_detail"].startswith("HTTP 401")
    assert "b" * 10 not in events[0].payload["error_detail"]


async def test_successful_calls_have_no_error_detail(permissions, usage, database, monkeypatch):
    install(monkeypatch, lambda req: httpx.Response(200, json=chat_response()))
    settings = configured()
    provider = AzureOpenAIProvider(settings, permissions)
    try:
        await ModelRouter([provider], BudgetGuard(usage, settings)).complete(
            CompletionRequest(messages=[{"role": "user", "content": "hi"}], tier=ModelTier.PREMIUM)
        )
    finally:
        await provider.aclose()
    _, events = await records(database)
    assert events[0].payload["error_detail"] is None
