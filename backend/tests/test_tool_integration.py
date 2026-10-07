"""Planner -> scheduler -> agents -> tools -> events -> state, on SQLite and PostgreSQL.

LLM: FakeLLMProvider. Tools: fake web search (FakeSearchBackend, results on
fake-search.invalid) and the real offline calculator. No network access.
"""

import asyncio
from uuid import UUID

import httpx

from app.agents.registry import AgentRegistry
from app.agents.runtime import AgentTaskExecutor
from app.core.config import Settings
from app.events.base import Event
from app.events.types import EventType
from app.llm.fake import FakeLLMProvider, FakeReply
from app.main import create_app
from app.orchestration.scheduler import Scheduler
from app.persistence.database import Database
from app.persistence.repositories import EventRepository
from app.services.planning import PlanningService
from app.services.runs import RunService
from app.state.models import TaskStatus, ToolCallStatus
from app.state.projector import apply, project
from app.tools.executor import ToolExecutor
from app.tools.fakes import FakeSearchBackend
from app.tools.policy import ToolPolicy
from tests.agent_fixtures import GOAL, RESEARCH_PLAN
from tests.tool_fixtures import call_tool, fact, fake_registry, finish, scripted_steps

S = TaskStatus
TIMEOUT = 10
T1, T2, T3 = "research_candidates", "research_alternatives", "compare_results"
Q1, Q2 = "candidate products", "alternative products"

STEPS = {
    T1: [
        call_tool("web_search", query=Q1, max_results=2),
        finish("Found candidates", [fact("Product A is a candidate.", "tool_output", f"{T1}.t1", "https://fake-search.invalid/candidate-products/1")]),
    ],
    T2: [
        call_tool("web_search", query=Q2, max_results=2),
        finish("Found alternatives", [fact("Product C is an alternative.", "tool_output", f"{T2}.t1", "https://fake-search.invalid/alternative-products/1")]),
    ],
    T3: [
        call_tool("calculator", expression="(1299 - 999) / 999 * 100"),
        finish(
            "A costs about 30% more than B",
            [fact("Product A costs 30.03% more than product B.", "tool_output", f"{T3}.t1"), fact("Price is not the only factor.")],
            artifacts=[{"name": "comparison.md", "media_type": "text/markdown", "content": "| A | B |\n|---|---|"}],
        ),
    ],
}


def provider(steps: dict = STEPS) -> FakeLLMProvider:  # type: ignore[type-arg]
    reply = scripted_steps(steps)
    return FakeLLMProvider({"planner": FakeReply(data=RESEARCH_PLAN), "agent:researcher": reply, "agent:analyst": reply})


def agent_registry(llm: FakeLLMProvider, search: FakeSearchBackend) -> AgentRegistry:
    executor = ToolExecutor(fake_registry(search), ToolPolicy())
    return AgentRegistry(llm, max_tokens=4000, tool_executor=executor, max_tool_calls=3)


async def planned_run(database: Database, registry: AgentRegistry) -> UUID:
    async with database.session_factory() as session:
        run_id = (await RunService(session).create_run(GOAL)).id
    async with database.session_factory() as session:
        await PlanningService(session, registry.planner(), max_tasks=10, timeout_seconds=TIMEOUT).plan_run(run_id)
    return run_id


async def events_of(database: Database, run_id: UUID) -> list[Event]:
    async with database.session_factory() as session:
        return await EventRepository(session).list_for_run(run_id)


def seq(events: list[Event], event_type: EventType, task_id: str) -> int:
    [match] = [e.sequence for e in events if e.event_type is event_type and getattr(e.payload, "task_id", None) == task_id]
    return match


async def test_end_to_end_research_with_search_then_calculator(database: Database) -> None:
    gates = {Q1: asyncio.Event(), Q2: asyncio.Event()}
    search = FakeSearchBackend(gates=gates)
    llm = provider()
    registry = agent_registry(llm, search)
    run_id = await planned_run(database, registry)
    scheduler = Scheduler(database, AgentTaskExecutor(registry, timeout_seconds=TIMEOUT))

    scheduling = asyncio.create_task(scheduler.run(run_id))
    await asyncio.wait_for(search.wait_until_searched(Q1), TIMEOUT)
    await asyncio.wait_for(search.wait_until_searched(Q2), TIMEOUT)
    assert search.active == {Q1, Q2}  # both researchers' tool calls in flight at once
    assert not any(r.purpose == "agent:analyst" for r in llm.requests)
    gates[Q1].set()
    gates[Q2].set()
    report = await asyncio.wait_for(scheduling, TIMEOUT)

    assert report.task_statuses == {T1: S.COMPLETED, T2: S.COMPLETED, T3: S.COMPLETED}
    assert search.max_active == 2

    events = await events_of(database, run_id)
    t3_start = seq(events, EventType.TASK_STARTED, T3)
    assert seq(events, EventType.TASK_COMPLETED, T1) < t3_start and seq(events, EventType.TASK_COMPLETED, T2) < t3_start

    tool_events = [e for e in events if e.event_type.value.startswith("Tool")]
    assert [(e.event_type.value, e.task_id, e.agent_id) for e in tool_events if e.task_id == T3] == [
        ("ToolCalled", T3, "analyst"), ("ToolSucceeded", T3, "analyst"),
    ]
    assert len(tool_events) == 6

    # Reconstruction: state rebuilt from the log alone equals the service's view.
    rebuilt = project(events)
    incremental = None
    for event in events:
        incremental = apply(incremental, event)
    async with database.session_factory() as session:
        assert await RunService(session).get_state(run_id) == rebuilt == incremental

    calls = rebuilt.tool_calls
    assert {c.tool_call_id: (c.tool_name, c.status, c.task_id) for c in calls.values()} == {
        f"{T1}.t1": ("web_search", ToolCallStatus.SUCCEEDED, T1),
        f"{T2}.t1": ("web_search", ToolCallStatus.SUCCEEDED, T2),
        f"{T3}.t1": ("calculator", ToolCallStatus.SUCCEEDED, T3),
    }
    assert calls[f"{T3}.t1"].output["result"] == 30.03003003003003  # type: ignore[index]
    assert calls[f"{T1}.t1"].metadata["fake"] is True and calls[f"{T3}.t1"].metadata["fake"] is False

    # Provenance.
    f1 = rebuilt.facts[f"{T1}.f1"]
    assert f1.provenance is not None
    assert (f1.provenance.kind, f1.provenance.tool_name, f1.provenance.fake, f1.agent_id, f1.task_id) == (
        "tool_output", "web_search", True, "researcher", T1,
    )
    calc_fact, model_fact = rebuilt.facts[f"{T3}.f1"], rebuilt.facts[f"{T3}.f2"]
    assert calc_fact.provenance.source == "calculator: (1299 - 999) / 999 * 100"  # type: ignore[union-attr]
    assert model_fact.provenance.kind == "model_knowledge" and model_fact.provenance.tool_call_id is None  # type: ignore[union-attr]

    # The analyst saw the researchers' tool-derived facts through its task context.
    first_analyst_request = next(r for r in llm.requests if r.purpose == "agent:analyst")
    assert "Product A is a candidate." in first_analyst_request.messages[0].content


async def test_unauthorized_tool_request_fails_the_task_and_is_recorded(database: Database) -> None:
    steps = {**STEPS, T1: [call_tool("calculator", expression="1+1"), finish()]}  # researcher may not
    registry = agent_registry(provider(steps), FakeSearchBackend())
    run_id = await planned_run(database, registry)

    report = await asyncio.wait_for(
        Scheduler(database, AgentTaskExecutor(registry, timeout_seconds=TIMEOUT)).run(run_id), TIMEOUT
    )

    assert report.task_statuses == {T1: S.FAILED, T2: S.COMPLETED, T3: S.BLOCKED}
    async with database.session_factory() as session:
        state = await RunService(session).get_state(run_id)
    call = state.tool_calls[f"{T1}.t1"]
    assert (call.tool_name, call.status, call.error_type) == ("calculator", ToolCallStatus.FAILED, "unauthorized")
    assert "[unauthorized]" in (state.tasks[T1].error or "")
    events = await events_of(database, run_id)
    t1 = [e.event_type.value for e in events if e.task_id == T1 and e.event_type is not EventType.TASK_CREATED]
    assert t1 == ["TaskStarted", "ToolCalled", "ToolFailed", "TaskFailed"]


async def test_tool_limit_failure_is_persisted_with_its_tool_events(database: Database) -> None:
    steps = {**STEPS, T3: [call_tool("calculator", expression="1+1")]}  # never finishes
    registry = agent_registry(provider(steps), FakeSearchBackend())
    run_id = await planned_run(database, registry)

    report = await asyncio.wait_for(
        Scheduler(database, AgentTaskExecutor(registry, timeout_seconds=TIMEOUT)).run(run_id), TIMEOUT
    )

    assert report.task_statuses[T3] == S.FAILED
    async with database.session_factory() as session:
        state = await RunService(session).get_state(run_id)
    assert state.tasks[T3].error == "tool-call limit of 3 per task reached"
    assert sum(1 for c in state.tool_calls.values() if c.task_id == T3) == 3


async def test_api_plan_and_schedule_with_tools(settings: Settings, database: Database) -> None:
    app = create_app(settings, database=database, llm_provider=provider(), tool_registry=fake_registry())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        run_id = (await api.post("/api/v1/runs", json={"goal": GOAL})).json()["id"]
        base = f"/api/v1/runs/{run_id}"
        assert (await api.post(f"{base}/plan")).status_code == 201
        scheduled = await api.post(f"{base}/schedule")
        assert scheduled.status_code == 200, scheduled.text
        assert set(scheduled.json()["task_statuses"].values()) == {"completed"}

        state = (await api.get(f"{base}/state")).json()
        assert {c["tool_name"] for c in state["tool_calls"].values()} == {"web_search", "calculator"}
        assert state["facts"][f"{T1}.f1"]["provenance"]["kind"] == "tool_output"
        assert state["facts"][f"{T1}.f1"]["provenance"]["fake"] is True
