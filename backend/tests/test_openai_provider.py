"""OpenAI-compatible adapter: settings, request shape, response parsing, error mapping, and
an unchanged planner/agent run through it.

The SDK call is replaced by a local stub returning real `ChatCompletion` objects, so these
tests make no network requests and need no credentials. All keys below are fake.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletion
from pydantic import ValidationError

from app.agents.planner import PlannerAgent
from app.agents.reasoning import REPORT_SCHEMA, step_schema
from app.agents.registry import TASK_AGENT_SPECS
from app.agents.replanner import ReplannerAgent
from app.agents.result import AgentReport, AgentStep
from app.core.config import Settings
from app.core.exceptions import LLMContentFilteredError, LLMError, LLMRateLimitError, LLMResponseError, LLMTimeoutError
from app.events.types import FailureType
from app.orchestration.task_executor import TaskExecutionResult
from app.recovery.classifier import classify_result
from app.events.types import FactClaim
from app.llm.fake import FakeLLMProvider
from app.llm.openai_provider import (
    SCHEMA_NAME,
    OpenAIProvider,
    is_strict_compatible,
    strict_wire_schema,
    strip_wire_nulls,
)
from app.llm.provider import build_provider
from app.llm.schemas import LLMMessage, LLMRequest
from app.main import create_app
from app.persistence.database import Database
from app.tools.calculator import CalculatorTool
from app.tools.http_fetch import HTTPFetchTool
from app.tools.registry import ToolRegistry
from tests.agent_fixtures import GOAL, RESEARCH_PLAN

FAKE_KEY = "sk-test-not-a-real-key"
BASE_URL = "https://llm.example.invalid/v1"
# Not natively strict-compatible ("answer" is optional): sent as a tightened strict schema.
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "additionalProperties": False}
TIGHTENED = {**SCHEMA, "properties": {"answer": {"anyOf": [{"type": "string"}, {"type": "null"}]}}, "required": ["answer"]}
STRICT_SCHEMA = {**SCHEMA, "required": ["answer"]}
# Cannot be made strict (free-form object): "auto" sends it non-strict.
FREEFORM_SCHEMA = {"type": "object", "properties": {"meta": {"type": "object"}}, "required": ["meta"], "additionalProperties": False}


def request(schema: dict[str, Any] = SCHEMA) -> LLMRequest:
    return LLMRequest(
        purpose="planner",
        system="system prompt",
        messages=[
            LLMMessage(role="user", content="hello"),
            LLMMessage(role="assistant", content='{"answer": "hi"}'),
            LLMMessage(role="user", content="again"),
        ],
        output_schema=schema,
        max_tokens=1000,
        metadata={"task_id": "t1"},
    )


def completion(
    content: str | None,
    *,
    finish_reason: str = "stop",
    refusal: str | None = None,
    model: str = "served-model",
    usage: bool = True,
    choices: bool = True,
) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": content, "refusal": refusal},
                }
            ]
            if choices
            else [],
            "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33} if usage else None,
        }
    )


Outcome = ChatCompletion | Exception | Callable[[dict[str, Any]], ChatCompletion]


def adapter(outcome: Outcome, **options: Any) -> tuple[OpenAIProvider, list[dict[str, Any]]]:
    provider = OpenAIProvider(
        model="configured-model", timeout_seconds=5, max_retries=0, api_key=FAKE_KEY, base_url=BASE_URL, **options
    )
    calls: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> ChatCompletion:
        calls.append(kwargs)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome(kwargs) if callable(outcome) else outcome

    provider._client.chat.completions.create = create  # type: ignore[method-assign]
    return provider, calls


def openai_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "NEXUS_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_LLM_PROVIDER", "openai")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


# --- Settings ------------------------------------------------------------------------------


def test_openai_provider_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = openai_settings(
        monkeypatch, NEXUS_LLM_MODEL="some-model", OPENAI_API_KEY=FAKE_KEY, OPENAI_BASE_URL=BASE_URL
    )

    provider = build_provider(settings)

    assert isinstance(provider, OpenAIProvider)
    assert provider.name == "openai" and provider._model == "some-model"
    assert str(provider._client.base_url).rstrip("/") == BASE_URL
    assert provider._client.api_key == FAKE_KEY
    # Defaults: json_schema with automatic strict mode, max_tokens.
    assert (provider._response_format, provider._strict_schema, provider._max_tokens_param) == (
        "json_schema", "auto", "max_tokens",
    )
    assert FAKE_KEY not in repr(settings)


def test_openai_options_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = openai_settings(
        monkeypatch,
        NEXUS_LLM_PROVIDER="OpenAI",
        NEXUS_LLM_MODEL="m",
        OPENAI_API_KEY=FAKE_KEY,
        NEXUS_OPENAI_RESPONSE_FORMAT="json_object",
        NEXUS_OPENAI_STRICT_SCHEMA="true",
        NEXUS_OPENAI_MAX_TOKENS_PARAM="max_completion_tokens",
    )

    provider = build_provider(settings)

    assert isinstance(provider, OpenAIProvider)
    assert (provider._response_format, provider._strict_schema, provider._max_tokens_param) == (
        "json_object", True, "max_completion_tokens",
    )


def test_openai_requires_an_explicit_model(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="NEXUS_LLM_MODEL must be set"):
        openai_settings(monkeypatch, OPENAI_API_KEY=FAKE_KEY)


def test_empty_openai_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = openai_settings(monkeypatch, NEXUS_LLM_MODEL="m", OPENAI_API_KEY="", OPENAI_BASE_URL=" ")

    assert settings.openai_api_key is None and settings.openai_base_url is None


@pytest.mark.parametrize(
    ("name", "value"),
    [("NEXUS_OPENAI_RESPONSE_FORMAT", "xml"), ("NEXUS_OPENAI_MAX_TOKENS_PARAM", "tokens")],
)
def test_invalid_openai_options_are_rejected(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    with pytest.raises(ValidationError):
        openai_settings(monkeypatch, NEXUS_LLM_MODEL="m", **{name: value})


@pytest.mark.parametrize(
    ("value", "expected"),
    [("auto", "auto"), ("AUTO", "auto"), ("false", False), ("False", False), ("true", True), ("1", True), ("0", False)],
)
def test_strict_schema_setting_is_backward_compatible(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool | str
) -> None:
    settings = openai_settings(monkeypatch, NEXUS_LLM_MODEL="m", NEXUS_OPENAI_STRICT_SCHEMA=value)

    assert settings.openai_strict_schema == expected
    assert type(settings.openai_strict_schema) is type(expected)


def test_invalid_strict_schema_setting_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        openai_settings(monkeypatch, NEXUS_LLM_MODEL="m", NEXUS_OPENAI_STRICT_SCHEMA="sometimes")


# --- Automatic strict mode -----------------------------------------------------------------


def obj(properties: dict[str, Any], required: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
        **extra,
    }


STR = {"type": "string"}


@pytest.mark.parametrize(
    "schema",
    [
        obj({"a": STR}),
        obj({}),
        obj({"a": {"anyOf": [STR, {"type": "null"}]}}),
        obj({"a": {"type": ["string", "null"]}, "b": {"type": "string", "enum": ["x", "y"]}, "c": {"const": "z"}}),
        obj({"a": {"type": "array", "items": obj({"b": STR})}}),
        obj({"a": {"$ref": "#/$defs/B"}}, **{"$defs": {"B": obj({"b": STR}, description="b")}}),
        obj({"a": {"type": ["object", "null"], "properties": {"b": STR}, "required": ["b"], "additionalProperties": False}}),
    ],
    ids=["flat", "empty", "nullable-anyof", "type-list-enum-const", "array-of-objects", "defs-ref", "nullable-object"],
)
def test_strict_compatible_schemas(schema: dict[str, Any]) -> None:
    assert is_strict_compatible(schema)


@pytest.mark.parametrize(
    "schema",
    [
        obj({"a": STR, "b": STR}, required=["a"]),
        {"type": "object", "properties": {"a": STR}, "required": ["a"]},
        {**obj({"a": STR}), "additionalProperties": True},
        obj({"a": {"type": "object"}}),
        obj({"a": {"type": "array", "items": obj({"b": STR, "c": STR}, required=["b"])}}),
        obj({"a": {"$ref": "#/$defs/B"}}, **{"$defs": {"B": obj({"b": STR}, required=[])}}),
        obj({"a": {"anyOf": [obj({"b": STR}, required=[]), {"type": "null"}]}}),
        obj({"a": {"oneOf": [STR]}}),
        obj({"a": {**STR, "default": "x"}}),
        {"anyOf": [obj({"a": STR})]},
        {"type": "array", "items": STR},
    ],
    ids=[
        "optional-property", "no-additional-properties", "open-object", "free-form-object", "array-item-optional",
        "defs-optional", "anyof-variant-optional", "unsupported-oneof", "unsupported-default", "root-anyof", "root-array",
    ],
)
def test_non_strict_schemas(schema: dict[str, Any]) -> None:
    assert not is_strict_compatible(schema)


def test_real_nexus_schemas_are_classified_as_expected() -> None:
    """Planner and replanner schemas qualify for strict mode natively; agent reports and
    tool-loop steps (optional fields) do not, but every one can be tightened into a strict
    schema. Schemas come from the unchanged agents."""
    tools = {"researcher": ["calculator"]}
    planner = PlannerAgent(FakeLLMProvider(), TASK_AGENT_SPECS, max_tokens=1000, tools_by_agent=tools)
    replanner = ReplannerAgent(FakeLLMProvider(), TASK_AGENT_SPECS, max_tokens=1000, tools_by_agent=tools)
    fetch = HTTPFetchTool(max_bytes=1024, max_timeout_seconds=5).definition

    assert is_strict_compatible(planner.output_schema())
    assert is_strict_compatible(replanner.output_schema())
    assert not is_strict_compatible(REPORT_SCHEMA)
    assert not is_strict_compatible(step_schema([CalculatorTool().definition]))
    assert not is_strict_compatible(step_schema([CalculatorTool().definition, fetch]))
    assert not is_strict_compatible(fetch.input_schema)

    assert strict_wire_schema(planner.output_schema()) == planner.output_schema()
    assert strict_wire_schema(replanner.output_schema()) == replanner.output_schema()
    for schema in (
        REPORT_SCHEMA,
        step_schema([CalculatorTool().definition]),
        step_schema([CalculatorTool().definition, fetch]),
        fetch.input_schema,
    ):
        wire = strict_wire_schema(schema)
        assert wire is not None and wire != schema and is_strict_compatible(wire)


async def test_auto_strict_mode_is_decided_per_request() -> None:
    provider, calls = adapter(lambda kw: completion('{"meta": {}}' if "meta" in kw["response_format"]["json_schema"]["schema"]["properties"] else '{"answer": "yes"}'))

    await provider.generate(request(STRICT_SCHEMA))
    await provider.generate(request(SCHEMA))
    await provider.generate(request(FREEFORM_SCHEMA))

    sent = [c["response_format"]["json_schema"] for c in calls]
    assert [j["strict"] for j in sent] == [True, True, False]
    assert sent[0]["schema"] == STRICT_SCHEMA  # natively strict: sent unchanged
    assert sent[1]["schema"] == TIGHTENED  # optional -> required + nullable
    assert sent[2]["schema"] == FREEFORM_SCHEMA  # cannot be strict: unchanged, non-strict


async def test_forced_strict_true_tightens_and_keeps_strict_for_unfixable_schemas() -> None:
    provider, calls = adapter(completion('{"answer": "yes"}'), strict_schema=True)

    await provider.generate(request(SCHEMA))

    sent = calls[0]["response_format"]["json_schema"]
    assert (sent["strict"], sent["schema"]) == (True, TIGHTENED)
    assert provider._request_kwargs(request(FREEFORM_SCHEMA))["response_format"]["json_schema"]["strict"] is True


async def test_strict_false_sends_every_schema_unchanged() -> None:
    provider, calls = adapter(completion('{"answer": null}'), strict_schema=False)

    response = await provider.generate(request(SCHEMA))

    sent = calls[0]["response_format"]["json_schema"]
    assert (sent["strict"], sent["schema"]) == (False, SCHEMA)
    assert response.data == {"answer": None}  # no normalization without tightening


# --- Tightening and null normalization ---------------------------------------------------


def test_tightening_makes_optional_properties_required_and_nullable() -> None:
    schema = obj(
        {
            "req": STR,
            "opt": STR,
            "opt_nullable": {"anyOf": [STR, {"type": "null"}]},
            "opt_type_list": {"type": ["string", "null"]},
            "opt_enum": {"type": "string", "enum": ["a", "b"], "description": "kept"},
        },
        required=["req"],
    )

    wire = strict_wire_schema(schema)

    assert wire is not None and is_strict_compatible(wire)
    assert wire["required"] == ["req", "opt", "opt_nullable", "opt_type_list", "opt_enum"]
    props = wire["properties"]
    assert props["req"] == STR  # required: unchanged, not nullable
    assert props["opt"] == {"anyOf": [STR, {"type": "null"}]}
    assert props["opt_nullable"] == schema["properties"]["opt_nullable"]  # already accepts null
    assert props["opt_type_list"] == schema["properties"]["opt_type_list"]
    assert props["opt_enum"] == {"anyOf": [{"type": "string", "enum": ["a", "b"], "description": "kept"}, {"type": "null"}]}
    assert schema["required"] == ["req"]  # the original is not mutated


def test_tightening_reaches_nested_objects_arrays_defs_and_anyof() -> None:
    inner = obj({"x": STR, "y": STR}, required=["x"])
    schema = obj(
        {
            "nested": inner,
            "items": {"type": "array", "items": inner},
            "ref": {"$ref": "#/$defs/Inner"},
            "choice": {"anyOf": [inner, {"type": "null"}]},
        },
        **{"$defs": {"Inner": inner}},
    )

    wire = strict_wire_schema(schema)

    assert wire is not None and is_strict_compatible(wire)
    tight_inner = {**inner, "properties": {"x": STR, "y": {"anyOf": [STR, {"type": "null"}]}}, "required": ["x", "y"]}
    assert wire["properties"]["nested"] == tight_inner
    assert wire["properties"]["items"]["items"] == tight_inner
    assert wire["$defs"]["Inner"] == tight_inner
    assert wire["properties"]["ref"] == {"$ref": "#/$defs/Inner"}
    assert wire["properties"]["choice"]["anyOf"][0] == tight_inner


def test_strip_removes_only_nulls_that_mean_absent() -> None:
    schema = obj(
        {"req_nullable": {"anyOf": [STR, {"type": "null"}]}, "opt": STR, "opt_nullable": {"anyOf": [STR, {"type": "null"}]}, "opt_set": STR},
        required=["req_nullable"],
    )

    data = {"req_nullable": None, "opt": None, "opt_nullable": None, "opt_set": "v"}

    assert strip_wire_nulls(data, schema) == {"req_nullable": None, "opt_nullable": None, "opt_set": "v"}
    assert data["opt"] is None  # input not mutated


def test_strip_never_adds_or_changes_values() -> None:
    schema = obj({"req": STR, "opt": STR}, required=["req"])

    # A missing required key, a null required key and an unknown key are left for
    # Pydantic to reject; nothing is invented or coerced.
    assert strip_wire_nulls({}, schema) == {}
    assert strip_wire_nulls({"req": None}, schema) == {"req": None}
    assert strip_wire_nulls({"req": "a", "extra": None}, schema) == {"req": "a", "extra": None}
    assert strip_wire_nulls({"req": "", "opt": 0}, schema) == {"req": "", "opt": 0}


def test_strip_follows_nested_objects_arrays_refs_and_anyof() -> None:
    inner = obj({"x": STR, "y": STR}, required=["x"])
    schema = obj(
        {"nested": inner, "items": {"type": "array", "items": {"$ref": "#/$defs/Inner"}}, "choice": {"anyOf": [inner, {"type": "null"}]}},
        **{"$defs": {"Inner": inner}},
    )
    data = {
        "nested": {"x": "a", "y": None},
        "items": [{"x": "b", "y": None}, {"x": "c", "y": "d"}],
        "choice": {"x": "e", "y": None},
    }

    assert strip_wire_nulls(data, schema) == {
        "nested": {"x": "a"},
        "items": [{"x": "b"}, {"x": "c", "y": "d"}],
        "choice": {"x": "e"},
    }
    assert strip_wire_nulls({**data, "choice": None}, schema)["choice"] is None


def test_strip_picks_the_anyof_variant_by_enum_and_leaves_ambiguity_alone() -> None:
    a = obj({"kind": {"type": "string", "enum": ["a"]}, "opt": STR}, required=["kind"])
    b = obj({"kind": {"type": "string", "enum": ["b"]}, "opt": {"anyOf": [STR, {"type": "null"}]}}, required=["kind"])
    schema = obj({"v": {"anyOf": [a, b]}})

    assert strip_wire_nulls({"v": {"kind": "a", "opt": None}}, schema) == {"v": {"kind": "a"}}
    assert strip_wire_nulls({"v": {"kind": "b", "opt": None}}, schema) == {"v": {"kind": "b", "opt": None}}
    twins = obj({"v": {"anyOf": [obj({"opt": STR}, required=[]), obj({"opt": STR}, required=[])]}})
    assert strip_wire_nulls({"v": {"opt": None}}, twins) == {"v": {"opt": None}}  # ambiguous: unchanged


def strict_shaped_report(**overrides: Any) -> dict[str, Any]:
    """An agent report as a strict server returns it for the tightened schema: every key
    present, absent optional values as null."""
    fact = {"content": "a finding", "basis": None, "tool_call_id": None, "source_url": None, "claim": None}
    claim_fact = {
        "content": "Product X costs 10 INR",
        "basis": "model_knowledge",
        "tool_call_id": None,
        "source_url": None,
        "claim": {"subject": "Product X", "attribute": "price", "value": 10, "unit": None},
    }
    return {
        "success": True,
        "summary": "done",
        "facts": [fact, claim_fact],
        "evidence": [{"source": "model_knowledge", "reference": None, "note": "general knowledge"}],
        "artifacts": [],
        "error": None,
        **overrides,
    }


async def test_agent_report_schema_round_trip_through_strict_mode() -> None:
    provider, calls = adapter(completion(json.dumps(strict_shaped_report())))

    response = await provider.generate(request(REPORT_SCHEMA))

    sent = calls[0]["response_format"]["json_schema"]
    assert sent["strict"] is True and is_strict_compatible(sent["schema"])
    facts = response.data["facts"]
    # `basis` is optional and not nullable in NEXUS's schema: its null meant "absent" and is
    # removed. tool_call_id / source_url / claim / unit accept null there: kept as sent.
    assert facts[0] == {"content": "a finding", "tool_call_id": None, "source_url": None, "claim": None}
    assert facts[1]["claim"] == {"subject": "Product X", "attribute": "price", "value": 10, "unit": None}
    assert response.data["error"] is None  # original schema allows null: kept
    report_model = AgentReport.model_validate(response.data)
    assert report_model.facts[0].basis == "model_knowledge"  # Pydantic's own default
    assert report_model.facts[1].claim is not None and report_model.facts[1].claim.unit is None


async def test_strict_mode_does_not_weaken_report_validation() -> None:
    """Values the original schema forbids still fail NEXUS validation after normalization."""
    bad = [
        strict_shaped_report(summary=None),  # required, not nullable: kept as null
        strict_shaped_report(success=False, error=None),  # model validator: error required
        {k: v for k, v in strict_shaped_report().items() if k != "evidence"},  # missing required key
    ]
    claim = strict_shaped_report()["facts"][1]
    bad.append(strict_shaped_report(facts=[{**claim, "claim": {**claim["claim"], "unit": ""}}]))  # minLength

    for payload in bad:
        provider, _ = adapter(completion(json.dumps(payload)))
        data = (await provider.generate(request(REPORT_SCHEMA))).data
        with pytest.raises(ValidationError):
            AgentReport.model_validate(data)


async def test_tool_step_schema_round_trip_through_strict_mode() -> None:
    fetch = HTTPFetchTool(max_bytes=1024, max_timeout_seconds=5).definition
    schema = step_schema([CalculatorTool().definition, fetch])
    step = {
        "action": "call_tool",
        "tool_call": {
            "tool_name": "http_fetch",
            "arguments": {"url": "https://example.invalid/", "method": None, "headers": None, "params": None, "timeout_seconds": None},
        },
        "report": None,
    }
    provider, calls = adapter(completion(json.dumps(step)))

    response = await provider.generate(request(schema))

    sent = calls[0]["response_format"]["json_schema"]
    assert sent["strict"] is True and is_strict_compatible(sent["schema"])
    assert response.data == {
        "action": "call_tool",
        "tool_call": {"tool_name": "http_fetch", "arguments": {"url": "https://example.invalid/"}},
        "report": None,  # required and nullable in the original: kept
    }
    parsed = AgentStep.model_validate(response.data)
    assert parsed.tool_call is not None and parsed.tool_call.tool_name == "http_fetch"
    assert fetch.name == "http_fetch"


def test_http_fetch_input_schema_tightening() -> None:
    fetch = HTTPFetchTool(max_bytes=1024, max_timeout_seconds=5).definition

    wire = strict_wire_schema(fetch.input_schema)

    assert wire is not None and is_strict_compatible(wire)
    assert wire["required"] == ["url", "method", "headers", "params", "timeout_seconds"]
    assert wire["properties"]["url"] == fetch.input_schema["properties"]["url"]
    assert wire["properties"]["method"] == {"anyOf": [fetch.input_schema["properties"]["method"], {"type": "null"}]}
    assert strip_wire_nulls({"url": "u", "method": None, "headers": None, "params": [], "timeout_seconds": 3}, fetch.input_schema) == {
        "url": "u", "params": [], "timeout_seconds": 3,
    }


# --- integer/number overlap (Groq: "integer_number_overlap") -------------------------------


def numeric_overlaps(node: Any) -> list[list[Any]]:
    """Every anyOf in `node` that lists both an integer and a number variant."""
    if isinstance(node, list):
        return [found for item in node for found in numeric_overlaps(item)]
    if not isinstance(node, dict):
        return []
    found = [v for v in node.values() for v in numeric_overlaps(v)]
    variants = node.get("anyOf")
    if isinstance(variants, list) and {"integer", "number"} <= {v.get("type") for v in variants if isinstance(v, dict)}:
        found.append(variants)
    return found


def test_fact_claim_value_wire_schema_drops_only_the_overlapping_integer() -> None:
    # StrictInt | StrictFloat | str: FactClaim's own type, as agents' report schema states it.
    assert FactClaim.model_json_schema()["properties"]["value"]["anyOf"][:2] == [{"type": "integer"}, {"type": "number"}]
    original = REPORT_SCHEMA["$defs"]["FactClaim"]["properties"]["value"]
    assert original == {"anyOf": [{"type": "integer"}, {"type": "number"}, {"type": "string"}]}

    wire = strict_wire_schema(REPORT_SCHEMA)

    assert wire is not None and is_strict_compatible(wire)
    assert wire["$defs"]["FactClaim"]["properties"]["value"] == {"anyOf": [{"type": "number"}, {"type": "string"}]}
    assert REPORT_SCHEMA["$defs"]["FactClaim"]["properties"]["value"] == original  # NEXUS schema not mutated


@pytest.mark.parametrize("schema", [REPORT_SCHEMA, step_schema([HTTPFetchTool(max_bytes=1024, max_timeout_seconds=5).definition])],
                         ids=["report", "step"])
def test_agent_wire_schemas_have_no_integer_number_overlap(schema: dict[str, Any]) -> None:
    assert numeric_overlaps(schema)  # the original keeps StrictInt | StrictFloat

    wire = strict_wire_schema(schema)

    assert wire is not None and is_strict_compatible(wire) and numeric_overlaps(wire) == []


@pytest.mark.parametrize(
    "variants",
    [
        [{"type": "integer"}, {"type": "string"}],  # no number: nothing overlaps
        [{"type": "number"}, {"type": "null"}],
        [{"type": "integer", "minimum": 1}, {"type": "number"}],  # constrained: not a plain overlap
        [{"type": "integer"}, {"type": "number", "maximum": 5}],
        [{"type": "integer", "enum": [1, 2]}, {"type": "number"}],
    ],
)
async def test_other_anyof_schemas_are_unchanged(variants: list[dict[str, Any]]) -> None:
    schema = {"type": "object", "properties": {"v": {"anyOf": variants}}, "required": ["v"], "additionalProperties": False}
    provider, calls = adapter(completion('{"v": 1}'))

    await provider.generate(request(schema))

    # Strict-sendable ones are sent as-is; constrained ones (minimum/maximum) cannot be
    # strict and go out non-strict, also as-is.
    assert calls[0]["response_format"]["json_schema"]["schema"]["properties"]["v"]["anyOf"] == variants


def test_a_strict_schema_without_overlap_is_still_sent_unchanged() -> None:
    planner = PlannerAgent(FakeLLMProvider(), TASK_AGENT_SPECS, max_tokens=100).output_schema()
    replanner = ReplannerAgent(FakeLLMProvider(), TASK_AGENT_SPECS, max_tokens=100).output_schema()

    assert strict_wire_schema(planner) is planner and strict_wire_schema(replanner) is replanner
    assert strict_wire_schema(STRICT_SCHEMA) is STRICT_SCHEMA


def test_a_strict_schema_with_overlap_gets_a_collapsed_copy() -> None:
    schema = {"type": "object", "properties": {"n": {"anyOf": [{"type": "integer"}, {"type": "number"}]}},
              "required": ["n"], "additionalProperties": False}

    wire = strict_wire_schema(schema)

    assert wire is not schema and wire is not None and wire["properties"]["n"] == {"anyOf": [{"type": "number"}]}
    assert schema["properties"]["n"]["anyOf"] == [{"type": "integer"}, {"type": "number"}]  # original not mutated


@pytest.mark.parametrize(("value", "kind"), [(10, int), (0, int), (-3, int), (2.5, float), (10.0, float), ("ten", str)])
async def test_claim_values_survive_the_collapsed_schema(value: Any, kind: type) -> None:
    claim_fact = strict_shaped_report()["facts"][1]
    report = strict_shaped_report(facts=[{**claim_fact, "claim": {**claim_fact["claim"], "value": value}}])
    provider, calls = adapter(completion(json.dumps(report)))

    response = await provider.generate(request(REPORT_SCHEMA))

    assert numeric_overlaps(calls[0]["response_format"]["json_schema"]["schema"]) == []
    claim = AgentReport.model_validate(response.data).facts[0].claim
    assert claim is not None and claim.value == value and type(claim.value) is kind  # original model, unchanged value


async def test_collapsed_schema_does_not_weaken_claim_validation() -> None:
    claim_fact = strict_shaped_report()["facts"][1]
    for value in (True, "", None, [1]):  # bool is not StrictInt; empty string; null; array
        report = strict_shaped_report(facts=[{**claim_fact, "claim": {**claim_fact["claim"], "value": value}}])
        provider, _ = adapter(completion(json.dumps(report)))
        data = (await provider.generate(request(REPORT_SCHEMA))).data
        with pytest.raises(ValidationError):
            AgentReport.model_validate(data)


async def test_schema_rejection_error_does_not_expose_the_key(caplog: pytest.LogCaptureFixture) -> None:
    provider, _ = adapter(status_error(openai.BadRequestError, 400))

    with pytest.raises(LLMError, match="400") as caught:
        await provider.generate(request(REPORT_SCHEMA))

    assert FAKE_KEY not in str(caught.value) and FAKE_KEY not in caplog.text


# --- Request shape -------------------------------------------------------------------------


async def test_request_shape() -> None:
    provider, calls = adapter(completion('{"answer": "yes"}'))

    response = await provider.generate(request(STRICT_SCHEMA))

    assert response.data == {"answer": "yes"}
    assert (response.provider, response.model) == ("openai", "served-model")
    assert (response.usage.input_tokens, response.usage.output_tokens) == (11, 22)
    [call] = calls
    assert call["model"] == "configured-model"
    assert call["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": '{"answer": "hi"}'},
        {"role": "user", "content": "again"},
    ]
    assert call["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": SCHEMA_NAME, "schema": STRICT_SCHEMA, "strict": True},
    }
    assert call["max_tokens"] == 1000 and "max_completion_tokens" not in call
    # Caller-side labels are never sent.
    assert "metadata" not in call and "planner" not in json.dumps(call) and "t1" not in json.dumps(call)


async def test_strict_schema_and_max_completion_tokens_options() -> None:
    provider, calls = adapter(
        completion('{"answer": "yes"}'), strict_schema=True, max_tokens_param="max_completion_tokens"
    )

    await provider.generate(request())

    [call] = calls
    assert call["response_format"]["json_schema"]["strict"] is True
    assert call["max_completion_tokens"] == 1000 and "max_tokens" not in call


async def test_json_object_mode_puts_the_schema_in_the_system_prompt() -> None:
    provider, calls = adapter(completion('{"answer": "yes"}'), response_format="json_object")

    assert (await provider.generate(request())).data == {"answer": "yes"}

    [call] = calls
    assert call["response_format"] == {"type": "json_object"}
    system = call["messages"][0]
    assert system["role"] == "system" and system["content"].startswith("system prompt\n\n")
    assert json.dumps(SCHEMA) in system["content"]


# --- Response parsing ----------------------------------------------------------------------


@pytest.mark.parametrize("text", ['```json\n{"answer": "yes"}\n```', '```\n{"answer": "yes"}\n```', '  {"answer": "yes"}\n'])
async def test_accepts_a_single_code_fence_or_whitespace(text: str) -> None:
    provider, _ = adapter(completion(text))

    assert (await provider.generate(request())).data == {"answer": "yes"}


async def test_missing_usage_and_model_fall_back() -> None:
    provider, _ = adapter(completion('{"answer": "yes"}', model="", usage=False))

    response = await provider.generate(request())

    assert response.model == "configured-model"
    assert (response.usage.input_tokens, response.usage.output_tokens) == (None, None)


REQ = httpx2.Request("POST", f"{BASE_URL}/chat/completions")


def status_error(cls: type[openai.APIStatusError], code: int) -> openai.APIStatusError:
    return cls("error", response=httpx2.Response(code, request=REQ), body=None)


@pytest.mark.parametrize(
    ("outcome", "error", "match"),
    [
        (openai.APITimeoutError(request=REQ), LLMTimeoutError, "timed out"),
        (status_error(openai.RateLimitError, 429), LLMError, "429"),
        (status_error(openai.AuthenticationError, 401), LLMError, "401"),
        (status_error(openai.BadRequestError, 400), LLMError, "400"),
        (status_error(openai.InternalServerError, 500), LLMError, "500"),
        (openai.APIConnectionError(request=REQ), LLMError, "could not reach"),
        (completion('{"answer": ', finish_reason="length"), LLMResponseError, "truncated"),
        (completion(None, finish_reason="content_filter"), LLMResponseError, "declined"),
        (completion(None, refusal="I can't help with that."), LLMResponseError, "declined"),
        (completion(None, choices=False), LLMResponseError, "no choices"),
        (completion(None), LLMResponseError, "no text"),
        (completion("not json"), LLMResponseError, "not valid JSON"),
        (completion("[1, 2]"), LLMResponseError, "not a JSON object"),
    ],
    ids=[
        "timeout", "rate-limit", "auth", "bad-request", "server", "connection", "truncated",
        "content-filter", "refusal", "no-choices", "no-content", "not-json", "not-object",
    ],
)
async def test_maps_failures_to_nexus_errors(outcome: Any, error: type[Exception], match: str) -> None:
    provider, _ = adapter(outcome)

    with pytest.raises(error, match=match) as caught:
        await provider.generate(request())
    assert FAKE_KEY not in str(caught.value)


# --- Unchanged planner + agents through the adapter ----------------------------------------


def scripted(kwargs: dict[str, Any]) -> ChatCompletion:
    """Answers by the schema NEXUS sent: the planner's has `tasks`, an agent's report has `summary`."""
    properties = kwargs["response_format"]["json_schema"]["schema"]["properties"]
    if "tasks" in properties:
        return completion(json.dumps(RESEARCH_PLAN))
    assert "summary" in properties
    return completion(json.dumps(strict_shaped_report()))


async def test_plan_and_schedule_through_the_openai_adapter(settings: Settings, database: Database) -> None:
    provider, calls = adapter(scripted)
    app = create_app(settings, database=database, llm_provider=provider, tool_registry=ToolRegistry())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        run_id = (await api.post("/api/v1/runs", json={"goal": GOAL})).json()["id"]
        base = f"/api/v1/runs/{run_id}"

        planned = await api.post(f"{base}/plan")
        assert planned.status_code == 201, planned.text
        assert (planned.json()["provider"], planned.json()["model"]) == ("openai", "served-model")

        scheduled = await api.post(f"{base}/schedule")
        assert scheduled.status_code == 200, scheduled.text
        assert set(scheduled.json()["task_statuses"].values()) == {"completed"}

    assert len(calls) == 1 + len(RESEARCH_PLAN["tasks"])
    # Automatic strict mode: the planner's schema natively, agent reports tightened.
    planner_call, *agent_calls = calls
    assert "tasks" in planner_call["response_format"]["json_schema"]["schema"]["properties"]
    assert planner_call["response_format"]["json_schema"]["strict"] is True
    assert [c["response_format"]["json_schema"]["strict"] for c in agent_calls] == [True] * len(agent_calls)
    assert all(c["response_format"]["json_schema"]["schema"] != REPORT_SCHEMA for c in agent_calls)


# --- Content filters (e.g. Gemini RECITATION) vs. empty responses -----------------------------


def filtered_completion(finish_reason: str) -> ChatCompletion:
    """A response with a provider-specific finish_reason the SDK passes through unchecked
    (Gemini's OpenAI-compatible API sends e.g. "content_filter: RECITATION")."""
    response = completion(None)
    response.choices[0].finish_reason = finish_reason  # type: ignore[assignment]
    return response


async def test_recitation_is_classified_as_content_filtered() -> None:
    provider, _ = adapter(filtered_completion("content_filter: RECITATION"))

    with pytest.raises(LLMContentFilteredError) as caught:
        await provider.generate(request())

    error = caught.value
    assert error.code == "llm_content_filtered" and isinstance(error, LLMResponseError)
    assert "content filter" in error.message and "no text" not in error.message
    assert "content_filter: RECITATION" in error.message  # the provider's reason is kept
    assert error.provider_reason == "finish_reason='content_filter: RECITATION'"
    assert classify_result(TaskExecutionResult.failure(error.message, error_type=error.code)).failure_type is (
        FailureType.VALIDATION_FAILURE  # recovery treats it as before
    )


async def test_refusal_text_is_kept_as_the_provider_reason() -> None:
    provider, _ = adapter(completion(None, refusal="I can't help with that."))

    with pytest.raises(LLMContentFilteredError) as caught:
        await provider.generate(request())
    assert "I can't help with that." in (caught.value.provider_reason or "")


async def test_empty_response_is_still_an_empty_response_error() -> None:
    provider, _ = adapter(completion(None))

    with pytest.raises(LLMResponseError) as caught:
        await provider.generate(request())
    assert not isinstance(caught.value, LLMContentFilteredError)
    assert caught.value.code == "llm_invalid_response" and "no text" in caught.value.message


async def test_successful_response_is_unchanged_by_the_filter_check() -> None:
    provider, _ = adapter(completion('{"answer": "hi"}'))

    response = await provider.generate(request())
    assert response.data == {"answer": "hi"}


async def test_http_429_is_a_rate_limit_error() -> None:
    provider, _ = adapter(status_error(openai.RateLimitError, 429))

    with pytest.raises(LLMRateLimitError) as caught:
        await provider.generate(request())
    assert caught.value.code == "llm_rate_limited" and "HTTP 429" in caught.value.message
    assert "not a task problem" in caught.value.message and FAKE_KEY not in caught.value.message


async def test_other_status_errors_stay_generic_llm_errors() -> None:
    provider, _ = adapter(status_error(openai.InternalServerError, 500))

    with pytest.raises(LLMError) as caught:
        await provider.generate(request())
    assert not isinstance(caught.value, LLMRateLimitError) and caught.value.code == "llm_error"


def test_narrowed_replanner_schemas_are_strict_compatible() -> None:
    replanner = ReplannerAgent(FakeLLMProvider(), TASK_AGENT_SPECS, max_tokens=100)
    for schema in (replanner.output_schema(failed_task_id="t1"), replanner.output_schema(remediation=True)):
        assert is_strict_compatible(schema)
