"""LLM provider layer: fake provider, schema preparation, Anthropic adapter error mapping.

The Anthropic adapter is exercised with its SDK client call replaced by a local stub, so
these tests make no network requests and need no credentials.
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.exceptions import LLMContentFilteredError, LLMError, LLMResponseError, LLMTimeoutError
from app.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider
from app.llm.fake import FakeLLMProvider, FakeReply
from app.llm.provider import build_provider
from app.llm.schemas import LLMMessage, LLMRequest, strict_json_schema


def request(purpose: str = "planner", **metadata: str) -> LLMRequest:
    return LLMRequest(
        purpose=purpose,
        system="system prompt",
        messages=[LLMMessage(role="user", content="hello")],
        output_schema={"type": "object", "properties": {}, "additionalProperties": False},
        max_tokens=1000,
        metadata=metadata,
    )


# --- Fake provider -------------------------------------------------------------------------


async def test_fake_provider_is_deterministic_and_records_requests() -> None:
    provider = FakeLLMProvider({"planner": FakeReply(data={"tasks": [{"id": "a"}]})})

    first = await provider.generate(request())
    second = await provider.generate(request())

    assert first == second
    assert first.data == {"tasks": [{"id": "a"}]}
    assert (first.provider, first.model) == ("fake", "fake-model")
    assert [r.purpose for r in provider.requests] == ["planner", "planner"]
    first.data["tasks"].append("mutated")  # type: ignore[union-attr]
    assert (await provider.generate(request())).data == {"tasks": [{"id": "a"}]}


async def test_fake_provider_failures() -> None:
    provider = FakeLLMProvider({"planner": FakeReply(error=LLMTimeoutError("slow"))})

    with pytest.raises(LLMTimeoutError):
        await provider.generate(request())
    with pytest.raises(LLMError, match="no scripted reply"):
        await provider.generate(request("agent:unknown"))


async def test_fake_provider_can_block_until_signalled() -> None:
    gate = asyncio.Event()
    provider = FakeLLMProvider({"agent:researcher": FakeReply(data={}, wait_for=gate)})

    call = asyncio.create_task(provider.generate(request("agent:researcher", task_id="t1")))
    await asyncio.wait_for(provider.wait_until_called("t1"), 5)
    assert provider.active == {"t1"} and not call.done()
    gate.set()
    await asyncio.wait_for(call, 5)
    assert provider.active == set()


# --- Structured-output schema --------------------------------------------------------------


class Inner(BaseModel):
    title: str = Field(min_length=1, max_length=10)


class Outer(BaseModel):
    items: list[Inner] = Field(max_length=3)
    note: str | None = Field(default=None, pattern="^x")


def test_strict_schema_uses_the_supported_subset() -> None:
    schema = strict_json_schema(Outer)
    text = json.dumps(schema)

    for keyword in ("minLength", "maxLength", "maxItems", "pattern", '"default"'):
        assert keyword not in text
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["Inner"]["additionalProperties"] is False
    assert "title" in schema["$defs"]["Inner"]["properties"]  # field named "title" kept


# --- Provider selection --------------------------------------------------------------------


def test_no_provider_unless_configured() -> None:
    assert build_provider(Settings(_env_file=None)) is None  # type: ignore[call-arg]


def test_anthropic_provider_from_settings_keeps_key_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    provider = build_provider(settings)

    assert isinstance(provider, AnthropicProvider)
    assert "sk-ant-test-not-real" not in repr(settings)
    assert settings.llm_model == "claude-opus-5"


# --- Anthropic adapter (SDK call stubbed; no network) --------------------------------------

REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def sdk_response(text: str | None, stop_reason: str = "end_turn") -> Any:
    content = [SimpleNamespace(type="thinking", thinking="")]
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=20),
        _request_id="req_test",
    )


def adapter(outcome: Any) -> tuple[AnthropicProvider, list[dict[str, Any]]]:
    provider = AnthropicProvider(model="claude-opus-5", timeout_seconds=5, max_retries=0, api_key="x")
    calls: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    provider._client.beta.messages.create = create  # type: ignore[method-assign]
    return provider, calls


async def test_anthropic_adapter_requests_structured_output() -> None:
    provider, calls = adapter(sdk_response('{"tasks": []}'))

    response = await provider.generate(request())

    assert response.data == {"tasks": []}
    assert (response.provider, response.model, response.usage.output_tokens) == ("anthropic", "claude-opus-5", 20)
    [call] = calls
    assert call["model"] == "claude-opus-5"
    assert call["output_config"] == {"format": {"type": "json_schema", "schema": request().output_schema}}
    assert call["fallbacks"] == "default" and call["betas"] == [FALLBACK_BETA]
    assert call["system"] == "system prompt"
    assert "metadata" not in call  # caller labels are never sent


@pytest.mark.parametrize(
    ("outcome", "error", "match"),
    [
        (anthropic.APITimeoutError(request=REQ), LLMTimeoutError, "timed out"),
        (anthropic.RateLimitError("limited", response=httpx2.Response(429, request=REQ), body=None), LLMError, "429"),
        (anthropic.APIConnectionError(request=REQ), LLMError, "could not reach"),
        (sdk_response(None, "refusal"), LLMContentFilteredError, "declined"),
        (sdk_response('{"tasks": [', "max_tokens"), LLMResponseError, "truncated"),
        (sdk_response("not json"), LLMResponseError, "not valid JSON"),
        (sdk_response("[1, 2]"), LLMResponseError, "not a JSON object"),
        (sdk_response(None), LLMResponseError, "no text block"),
    ],
    ids=["timeout", "rate-limit", "connection", "refusal", "truncated", "not-json", "not-object", "no-text"],
)
async def test_anthropic_adapter_maps_failures(outcome: Any, error: type[Exception], match: str) -> None:
    provider, _ = adapter(outcome)

    with pytest.raises(error, match=match):
        await provider.generate(request())
