"""Role-based LLM routing: deterministic configuration -> provider/model per role.

FakeLLMProvider stands in for every provider; OpenAIProvider instances are built but
never called. No network, no real LLM. All keys below are fake.
"""

import logging
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents.registry import AgentRegistry
from app.agents.runtime import AgentTaskExecutor
from app.core.config import LLM_ROLES, Settings
from app.core.exceptions import LLMConfigurationError, LLMError
from app.events.types import RunCreated, TaskCreated
from app.llm.fake import FakeLLMProvider, FakeReply
from app.llm.openai_provider import OpenAIProvider
from app.llm.provider import build_provider
from app.llm.routing import RoutingProvider, configured_routes, resolve_llm_config, role_for, routing_problems
from app.llm.schemas import LLMMessage, LLMRequest
from app.state.context_builder import build_task_context
from app.state.projector import project
from app.tools.executor import ToolExecutor
from app.tools.policy import ToolPolicy
from app.tools.registry import ToolRegistry
from tests.agent_fixtures import AGENT_REPORTS, agent_reply
from tests.helpers import history
from tests.test_conflicts import conflicting

GEMINI_KEY = "AIza-fake-gemini-key-not-real-000"
OPENROUTER_KEY = "sk-or-fake-openrouter-key-not-real"
GROQ_KEY = "gsk_fake-groq-key-not-real-0000"
SECRETS = (GEMINI_KEY, OPENROUTER_KEY, GROQ_KEY)
BASE_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
}

# The configured routing matrix.
ROUTES = {
    "planner": ("groq", "openai/gpt-oss-120b"),
    "researcher": ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free"),
    "analyst": ("openrouter", "qwen/qwen3.8-27b:free"),
    "specialist": ("gemini", "gemini-3.5-flash-lite"),
    "conflict_resolver": ("openrouter", "qwen/qwen3.8-27b:free"),
    "replanner": ("groq", "openai/gpt-oss-20b"),
    "verifier": ("groq", "openai/gpt-oss-120b"),
}
GROQ_ROLES = ("planner", "replanner", "verifier")


def routed_settings(monkeypatch: pytest.MonkeyPatch, routes: dict[str, tuple[str, str | None]] = ROUTES, **env: str) -> Settings:
    for role in LLM_ROLES:
        for suffix in ("PROVIDER", "MODEL"):
            monkeypatch.delenv(f"NEXUS_{role.upper()}_{suffix}", raising=False)
    for name in ("NEXUS_GEMINI_API_KEY", "NEXUS_OPENROUTER_API_KEY", "NEXUS_GROQ_API_KEY", "NEXUS_LLM_PROVIDER", "NEXUS_LLM_MODEL", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    for role, (provider, model) in routes.items():
        monkeypatch.setenv(f"NEXUS_{role.upper()}_PROVIDER", provider)
        if model is not None:
            monkeypatch.setenv(f"NEXUS_{role.upper()}_MODEL", model)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def keys() -> dict[str, str]:
    return {"NEXUS_GEMINI_API_KEY": GEMINI_KEY, "NEXUS_OPENROUTER_API_KEY": OPENROUTER_KEY, "NEXUS_GROQ_API_KEY": GROQ_KEY}


def request(purpose: str, **metadata: str) -> LLMRequest:
    return LLMRequest(purpose=purpose, system="s", messages=[LLMMessage(role="user", content="u")],
                      output_schema={"type": "object"}, metadata=metadata)


# --- 1-7: each role resolves to its configured provider/model -------------------------------


@pytest.mark.parametrize("role", LLM_ROLES)
def test_role_resolves_to_configured_provider_and_model(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    settings = routed_settings(monkeypatch, **keys())
    config = resolve_llm_config(settings, role)
    provider, model = ROUTES[role]
    assert (config.provider, config.model) == (provider, model)
    assert config.base_url == BASE_URLS[provider]


@pytest.mark.parametrize(
    ("purpose", "metadata", "role"),
    [
        ("planner", {}, "planner"),
        ("agent:researcher", {"task_id": "t"}, "researcher"),
        ("agent:analyst", {"task_id": "t"}, "analyst"),
        ("agent:specialist", {"task_id": "t"}, "specialist"),
        ("agent:researcher", {"task_id": "resolve_c", "conflict_id": "c"}, "conflict_resolver"),
        ("replanner", {}, "replanner"),
        ("verifier", {}, "verifier"),
        ("smoke:ping", {}, None),
    ],
)
def test_requests_map_to_roles(purpose: str, metadata: dict[str, str], role: str | None) -> None:
    assert role_for(request(purpose, **metadata)) == role


async def test_each_role_is_answered_by_its_own_provider() -> None:
    providers = {role: FakeLLMProvider({p: FakeReply(data={"role": role}) for p in ("planner", "replanner", "verifier", "agent:researcher", "agent:analyst", "agent:specialist")})
                 for role in LLM_ROLES}
    router = RoutingProvider(providers, labels={r: ROUTES[r][0] for r in LLM_ROLES})
    for purpose, metadata, role in [("planner", {}, "planner"), ("agent:researcher", {}, "researcher"), ("agent:analyst", {}, "analyst"),
                                    ("agent:specialist", {}, "specialist"), ("agent:researcher", {"conflict_id": "c"}, "conflict_resolver"),
                                    ("replanner", {}, "replanner"), ("verifier", {}, "verifier")]:
        response = await router.generate(request(purpose, **metadata))
        assert response.data == {"role": role} and response.provider == ROUTES[role][0]
    assert all(len(p.requests) == 1 for p in providers.values())


def test_routed_adapters_are_built_with_the_configured_endpoint_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    router = build_provider(routed_settings(monkeypatch, **keys()))
    assert isinstance(router, RoutingProvider)
    researcher = router.provider_for(request("agent:researcher"))[1]
    planner = router.provider_for(request("planner"))[1]
    assert isinstance(researcher, OpenAIProvider) and isinstance(planner, OpenAIProvider)
    assert (researcher._model, str(researcher._client.base_url).rstrip("/")) == (ROUTES["researcher"][1], "https://openrouter.ai/api/v1")
    assert planner._model == "openai/gpt-oss-120b" and str(planner._client.base_url).rstrip("/") == BASE_URLS["groq"]
    assert planner is router.provider_for(request("verifier"))[1]  # same provider + model: one adapter
    resolver_role, resolver = router.provider_for(request("agent:researcher", conflict_id="c"))
    analyst = router.provider_for(request("agent:analyst"))[1]
    assert resolver_role == "conflict_resolver" and isinstance(resolver, OpenAIProvider)
    assert (resolver._model, str(resolver._client.base_url).rstrip("/")) == ("qwen/qwen3.8-27b:free", "https://openrouter.ai/api/v1")
    assert resolver is analyst and resolver is not researcher  # same provider + model as the analyst: one adapter
    specialist = router.provider_for(request("agent:specialist"))[1]
    assert specialist._model == "gemini-3.5-flash-lite" and "generativelanguage.googleapis.com" in str(specialist._client.base_url)  # type: ignore[union-attr]


async def test_conflict_resolution_requests_carry_the_conflict_id() -> None:
    log = conflicting().detect()
    state = log.state()
    [conflict] = state.conflicts.values()
    assert conflict.resolution_task_id is not None
    llm = FakeLLMProvider({"agent:researcher": FakeReply(data=AGENT_REPORTS["research_candidates"])})
    agent = AgentRegistry(llm, max_tokens=1000).resolve("researcher")
    await agent.run(build_task_context(state, conflict.resolution_task_id), None)
    await agent.run(build_task_context(state, "ra"), None)
    resolver_request, plain_request = llm.requests
    assert resolver_request.metadata["conflict_id"] == conflict.conflict_id and role_for(resolver_request) == "conflict_resolver"
    assert "conflict_id" not in plain_request.metadata and role_for(plain_request) == "researcher"


# --- 8: backward compatibility --------------------------------------------------------------


def test_without_role_settings_the_existing_provider_is_used_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = routed_settings(monkeypatch, routes={}, NEXUS_LLM_PROVIDER="openai", NEXUS_LLM_MODEL="gpt-4.1-nano",
                               OPENAI_API_KEY="sk-fake-navigate", OPENAI_BASE_URL="https://llm.example.invalid")
    provider = build_provider(settings)
    assert type(provider) is OpenAIProvider and provider._model == "gpt-4.1-nano"  # type: ignore[union-attr]
    assert configured_routes(settings) == {}
    assert build_provider(routed_settings(monkeypatch, routes={})) is None  # still: no provider unless configured


async def test_unrouted_roles_fall_back_to_the_default_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = routed_settings(monkeypatch, routes={"verifier": ("gemini", "gemini-3.8-flash")}, NEXUS_GEMINI_API_KEY=GEMINI_KEY,
                               NEXUS_LLM_PROVIDER="openai", NEXUS_LLM_MODEL="gpt-4.1-nano", OPENAI_API_KEY="sk-fake-navigate")
    router = build_provider(settings)
    assert isinstance(router, RoutingProvider)
    default = router.provider_for(request("planner"))[1]
    assert type(default) is OpenAIProvider and default._model == "gpt-4.1-nano"  # type: ignore[union-attr]
    assert router.provider_for(request("verifier"))[1]._model == "gemini-3.8-flash"  # type: ignore[union-attr]
    # "default" can also be chosen explicitly (e.g. keep the Navigate Labs model for one role).
    explicit = routed_settings(monkeypatch, routes={"planner": ("default", None)}, NEXUS_LLM_PROVIDER="openai",
                               NEXUS_LLM_MODEL="gpt-4.1-nano", OPENAI_API_KEY="sk-fake-navigate")
    assert build_provider(explicit).provider_for(request("planner"))[1]._model == "gpt-4.1-nano"  # type: ignore[union-attr]


# --- 9-12: validation and secrets -----------------------------------------------------------


def test_missing_key_of_an_unused_provider_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    only_gemini = {r: ("gemini", "gemini-3.5-flash-lite") for r in LLM_ROLES}
    settings = routed_settings(monkeypatch, routes=only_gemini, NEXUS_GEMINI_API_KEY=GEMINI_KEY)  # no OpenRouter / Groq key
    router = build_provider(settings)
    assert isinstance(router, RoutingProvider) and routing_problems(settings) == []
    assert all(isinstance(router.provider_for(request(p))[1], OpenAIProvider) for p in ("planner", "agent:analyst", "verifier"))


@pytest.mark.parametrize(
    ("env", "provider", "purpose"),
    [({"NEXUS_GEMINI_API_KEY": GEMINI_KEY, "NEXUS_GROQ_API_KEY": GROQ_KEY}, "openrouter", "agent:researcher"),
     ({"NEXUS_OPENROUTER_API_KEY": OPENROUTER_KEY, "NEXUS_GROQ_API_KEY": GROQ_KEY}, "gemini", "agent:specialist"),
     ({"NEXUS_GEMINI_API_KEY": GEMINI_KEY, "NEXUS_OPENROUTER_API_KEY": OPENROUTER_KEY}, "groq", "planner")],
)
async def test_missing_key_of_a_selected_provider_fails_clearly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, env: dict[str, str], provider: str, purpose: str
) -> None:
    settings = routed_settings(monkeypatch, **env)
    message = f"routed to {provider}, but NEXUS_{provider.upper()}_API_KEY is not set"
    assert any(message in p for p in routing_problems(settings))
    with caplog.at_level(logging.WARNING):
        router = build_provider(settings)  # the app still starts; the problem is logged
    assert message in caplog.text
    with pytest.raises(LLMConfigurationError, match=message) as caught:
        await router.generate(request(purpose))  # type: ignore[union-attr]  # fails before anything is sent
    assert isinstance(caught.value, LLMError) and caught.value.code == "llm_misconfigured"
    leaked = str(caught.value) + caplog.text
    assert not any(secret in leaked for secret in SECRETS)
    other = "agent:researcher" if provider != "openrouter" else "agent:specialist"  # other providers' roles still work
    assert isinstance(router.provider_for(request(other))[1], OpenAIProvider)  # type: ignore[union-attr]


async def test_default_route_without_a_default_provider_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = routed_settings(monkeypatch, routes={"planner": ("default", None)})
    assert routing_problems(settings) == ["NEXUS_PLANNER_PROVIDER=default, but no default provider is configured (NEXUS_LLM_PROVIDER)"]
    with pytest.raises(LLMConfigurationError, match="no default provider is configured"):
        await build_provider(settings).generate(request("planner"))  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("routes", "env", "match"),
    [
        ({"planner": ("anthropic-free", "x")}, {}, "Input should be 'default', 'gemini', 'openrouter' or 'groq'"),
        ({"planner": ("groq", None)}, {}, "NEXUS_PLANNER_PROVIDER=groq requires NEXUS_PLANNER_MODEL"),
        ({"planner": ("gemini", None)}, {}, "NEXUS_PLANNER_PROVIDER=gemini requires NEXUS_PLANNER_MODEL"),
        ({}, {"NEXUS_VERIFIER_MODEL": "gemini-3.8-flash"}, "NEXUS_VERIFIER_MODEL is set but NEXUS_VERIFIER_PROVIDER is not"),
        ({}, {"NEXUS_OPENROUTER_BASE_URL": "http://openrouter.ai/api/v1"}, "must use https://"),
        ({}, {"NEXUS_GROQ_BASE_URL": "http://api.groq.com/openai/v1"}, "must use https://"),
    ],
    ids=["unknown-provider", "missing-model", "groq-missing-model", "model-without-provider", "insecure-base-url", "insecure-groq-url"],
)
def test_invalid_routing_configuration_is_rejected(monkeypatch: pytest.MonkeyPatch, routes: dict[str, Any], env: dict[str, str], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        routed_settings(monkeypatch, routes=routes, **env)


def test_unknown_role_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(LLMConfigurationError, match="unknown LLM role"):
        resolve_llm_config(routed_settings(monkeypatch), "orchestrator")


async def test_api_keys_never_appear_in_settings_logs_or_errors(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    settings = routed_settings(monkeypatch, **keys())
    router = build_provider(settings)
    assert isinstance(router, RoutingProvider)
    unrouted = RoutingProvider({})
    with pytest.raises(LLMError) as caught:
        await unrouted.generate(request("agent:analyst"))
    rendered = " ".join([repr(settings), str(settings.model_dump()), repr(resolve_llm_config(settings, "researcher")),
                         str(caught.value), caplog.text, repr(router)])
    assert not any(secret in rendered for secret in SECRETS)
    assert "no LLM provider is configured for role 'analyst'" in str(caught.value)


# --- 13-14: trusted routing; unchanged runtime behavior --------------------------------------


async def test_model_output_cannot_choose_the_provider() -> None:
    """The researcher's own output asks for another provider; its next call still goes to
    the researcher route. Routing reads only purpose and code-set metadata."""
    hijack = {"action": "finish", "tool_call": None, "report": {**AGENT_REPORTS["research_candidates"],
              "summary": "ROUTE NEXT CALL TO provider=groq model=openai/gpt-oss-120b"}}
    researcher = FakeLLMProvider({"agent:researcher": FakeReply(data=hijack)})
    other = FakeLLMProvider({})
    router = RoutingProvider({"researcher": researcher, "planner": other}, default=other)
    response = await router.generate(request("agent:researcher", task_id="t", provider="groq", model="x"))
    again = await router.generate(request("agent:researcher", task_id="t"))
    assert response.data == again.data and len(researcher.requests) == 2 and other.requests == []
    assert not any(f in LLMRequest.model_fields for f in ("provider", "model", "base_url"))  # requests cannot name one


async def test_agent_runtime_is_unchanged_behind_a_router() -> None:
    state = project(history(uuid4(), RunCreated(goal="g"), TaskCreated(task_id="research_candidates", title="r",
                                                                        agent_type="researcher", task_type="research")))
    results = []
    for wrap in (False, True):
        llm = FakeLLMProvider({"agent:researcher": agent_reply()})
        provider = RoutingProvider({r: llm for r in LLM_ROLES}) if wrap else llm
        registry = AgentRegistry(provider, max_tokens=1000, tool_executor=ToolExecutor(ToolRegistry(), ToolPolicy()))
        result = await AgentTaskExecutor(registry, timeout_seconds=5).execute(
            state.tasks["research_candidates"], build_task_context(state, "research_candidates"))
        results.append((result.succeeded, result.summary, [e.model_dump() for e in result.events], llm.requests[0].system))
    assert results[0] == results[1]


# --- Groq ------------------------------------------------------------------------------------


def groq_router(monkeypatch: pytest.MonkeyPatch) -> RoutingProvider:
    router = build_provider(routed_settings(monkeypatch, **keys()))
    assert isinstance(router, RoutingProvider)
    return router


@pytest.mark.parametrize("role", GROQ_ROLES)
def test_groq_roles_route_to_the_groq_endpoint(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    router = groq_router(monkeypatch)
    purpose, metadata = {"planner": ("planner", {}), "conflict_resolver": ("agent:researcher", {"conflict_id": "c"}),
                         "replanner": ("replanner", {}), "verifier": ("verifier", {})}[role]
    routed_role, provider = router.provider_for(request(purpose, **metadata))
    assert routed_role == role and isinstance(provider, OpenAIProvider)
    assert (provider._model, str(provider._client.base_url).rstrip("/")) == (ROUTES[role][1], BASE_URLS["groq"])
    assert provider._client.api_key == GROQ_KEY  # handed to the SDK, never logged or stored in state
    assert resolve_llm_config(routed_settings(monkeypatch, **keys()), role).provider == "groq"


def test_groq_models_share_one_adapter_per_model(monkeypatch: pytest.MonkeyPatch) -> None:
    router = groq_router(monkeypatch)
    big = router.provider_for(request("planner"))[1]
    small = router.provider_for(request("replanner"))[1]
    assert big is router.provider_for(request("verifier"))[1]
    assert big is not small


def completion(content: str | None, finish_reason: str = "stop") -> Any:
    from openai.types.chat import ChatCompletion

    return ChatCompletion.model_validate({
        "id": "x", "object": "chat.completion", "created": 0, "model": "openai/gpt-oss-120b",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    })


def stub_groq(router: RoutingProvider, outcome: Any) -> list[dict[str, Any]]:
    provider = router.provider_for(request("planner"))[1]
    calls: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    provider._client.chat.completions.create = create  # type: ignore[union-attr,method-assign]
    return calls


async def test_groq_request_is_strict_json_schema_without_native_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agents.reasoning import step_schema
    from app.tools.http_fetch import HTTPFetchTool

    router = groq_router(monkeypatch)
    calls = stub_groq(router, completion('{"action": "finish", "tool_call": null, "report": null}'))
    schema = step_schema([HTTPFetchTool(max_bytes=1024, max_timeout_seconds=15).definition])
    response = await router.generate(LLMRequest(purpose="planner", system="s", messages=[LLMMessage(role="user", content="u")],
                                                output_schema=schema, max_tokens=900, metadata={"task_id": "t"}))
    [call] = calls
    assert call["model"] == "openai/gpt-oss-120b" and call["max_tokens"] == 900
    assert call["response_format"]["type"] == "json_schema" and call["response_format"]["json_schema"]["strict"] is True
    assert "tools" not in call and "tool_choice" not in call and "metadata" not in call  # NEXUS runs tools itself
    assert response.provider == "groq" and response.model == "openai/gpt-oss-120b"


@pytest.mark.parametrize(
    ("status", "error_cls", "match"),
    [(429, "RateLimitError", "429"), (503, "InternalServerError", "503"), (401, "AuthenticationError", "401"),
     (400, "BadRequestError", "400")],
)
async def test_groq_errors_map_to_nexus_errors(monkeypatch: pytest.MonkeyPatch, status: int, error_cls: str, match: str) -> None:
    import httpx2
    import openai

    router = groq_router(monkeypatch)
    req = httpx2.Request("POST", f"{BASE_URLS['groq']}/chat/completions")
    stub_groq(router, getattr(openai, error_cls)("error", response=httpx2.Response(status, request=req), body=None))
    with pytest.raises(LLMError, match=match) as caught:
        await router.generate(request("planner"))
    assert GROQ_KEY not in str(caught.value)


async def test_groq_timeout_and_truncation_map_to_nexus_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx2
    import openai

    from app.core.exceptions import LLMResponseError, LLMTimeoutError

    router = groq_router(monkeypatch)
    stub_groq(router, openai.APITimeoutError(request=httpx2.Request("POST", BASE_URLS["groq"])))
    with pytest.raises(LLMTimeoutError):
        await router.generate(request("planner"))
    stub_groq(router, completion('{"tasks": [', finish_reason="length"))
    with pytest.raises(LLMResponseError, match="truncated"):
        await router.generate(request("planner"))
