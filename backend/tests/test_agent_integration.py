"""Goal -> planner -> validated graph -> scheduler -> agents -> events -> state.

Runs on SQLite and (when configured) PostgreSQL. The LLM is always the deterministic fake
provider; no external service is called.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
import pytest

from app.agents.registry import AgentRegistry
from app.agents.runtime import AgentTaskExecutor
from app.core.config import Settings
from app.core.exceptions import LLMError, LLMTimeoutError, PlanAlreadyExistsError, PlanRejectedError
from app.events.base import Event
from app.events.types import EventType, FactAdded, RunCompleted
from app.llm.fake import FakeLLMProvider, FakeReply
from app.main import create_app
from app.orchestration.scheduler import Scheduler
from app.orchestration.task_executor import TaskExecutionResult
from app.persistence.database import Database
from app.persistence.repositories import EventRepository
from app.services.planning import PlanningService
from app.services.runs import RunService
from app.state.context_builder import TaskContext
from app.state.models import TaskState, TaskStatus
from app.state.projector import apply, project
from app.tools.registry import ToolRegistry
from tests.agent_fixtures import GOAL, RESEARCH_PLAN, planned, research_provider

S = TaskStatus
TIMEOUT = 10
T1, T2, T3 = "research_candidates", "research_alternatives", "compare_results"


async def new_run(database: Database, constraints: tuple[str, ...] = ()) -> UUID:
    async with database.session_factory() as session:
        return (await RunService(session).create_run(GOAL, constraints)).id


async def plan(database: Database, provider: FakeLLMProvider, run_id: UUID, *, max_tasks: int = 10, timeout: float = TIMEOUT) -> list[TaskState]:
    async with database.session_factory() as session:
        service = PlanningService(
            session,
            AgentRegistry(provider, max_tokens=4000).planner(),
            max_tasks=max_tasks,
            timeout_seconds=timeout,
        )
        return (await service.plan_run(run_id)).tasks


async def events_of(database: Database, run_id: UUID) -> list[Event]:
    async with database.session_factory() as session:
        return await EventRepository(session).list_for_run(run_id)


def seq(events: list[Event], event_type: EventType, task_id: str) -> int:
    [match] = [e.sequence for e in events if e.event_type is event_type and getattr(e.payload, "task_id", None) == task_id]
    return match


def scheduler(database: Database, provider: FakeLLMProvider, timeout: float = TIMEOUT) -> Scheduler:
    executor = AgentTaskExecutor(AgentRegistry(provider, max_tokens=4000), timeout_seconds=timeout)
    return Scheduler(database, executor)


# --- Planning ------------------------------------------------------------------------------


async def test_goal_to_planned_task_graph(database: Database) -> None:
    run_id = await new_run(database, ("Budget under $500",))
    provider = research_provider()

    tasks = await plan(database, provider, run_id)

    assert {t.task_id: t.status for t in tasks} == {T1: S.READY, T2: S.READY, T3: S.PENDING}
    assert {t.task_id: t.agent_type for t in tasks} == {T1: "researcher", T2: "researcher", T3: "analyst"}
    events = await events_of(database, run_id)
    created = [e for e in events if e.event_type is EventType.TASK_CREATED]
    assert len(created) == 3 and {e.agent_id for e in created} == {"planner"}
    assert "Budget under $500" in provider.requests[0].messages[0].content


@pytest.mark.parametrize(
    "bad_plan",
    [
        {"tasks": [planned("a", dependencies=["b"]), planned("b", dependencies=["a"])]},
        {"tasks": [planned(f"t{i}") for i in range(4)]},  # over max_tasks=3
        {"tasks": [planned("a", agent_type="unknown")]},
        {"not": "a plan"},
    ],
    ids=["cycle", "oversized", "unknown-agent", "malformed"],
)
async def test_rejected_plan_writes_nothing(database: Database, bad_plan: dict) -> None:  # type: ignore[type-arg]
    run_id = await new_run(database)
    provider = FakeLLMProvider({"planner": FakeReply(data=bad_plan)})

    with pytest.raises(PlanRejectedError):
        await plan(database, provider, run_id, max_tasks=3)

    assert [e.event_type for e in await events_of(database, run_id)] == [EventType.RUN_CREATED]


async def test_plan_once_per_run(database: Database) -> None:
    run_id = await new_run(database)
    await plan(database, research_provider(), run_id)

    with pytest.raises(PlanAlreadyExistsError):
        await plan(database, research_provider(), run_id)


async def test_planner_failure_and_timeout_write_nothing(database: Database) -> None:
    run_id = await new_run(database)
    with pytest.raises(LLMError):
        await plan(database, FakeLLMProvider({"planner": FakeReply(error=LLMError("down"))}), run_id)
    with pytest.raises(LLMTimeoutError, match="planner did not finish"):
        hung = FakeLLMProvider({"planner": FakeReply(data=RESEARCH_PLAN, wait_for=asyncio.Event())})
        await plan(database, hung, run_id, timeout=0.05)

    assert len(await events_of(database, run_id)) == 1


# --- End-to-end: planner -> scheduler -> agents --------------------------------------------


async def test_end_to_end_research_and_compare(database: Database) -> None:
    """Spec scenario: T1 and T2 (researchers) run concurrently; T3 (analyst) waits for both."""
    run_id = await new_run(database)
    gates = {T1: asyncio.Event(), T2: asyncio.Event()}
    provider = research_provider(gates=gates)
    await plan(database, provider, run_id)

    scheduling = asyncio.create_task(scheduler(database, provider).run(run_id))
    await asyncio.wait_for(provider.wait_until_called(T1), TIMEOUT)
    await asyncio.wait_for(provider.wait_until_called(T2), TIMEOUT)
    assert provider.active == {T1, T2}  # both researcher calls in flight at once
    assert [r.purpose for r in provider.requests].count("agent:analyst") == 0
    gates[T1].set()
    gates[T2].set()
    report = await asyncio.wait_for(scheduling, TIMEOUT)

    assert report.task_statuses == {T1: S.COMPLETED, T2: S.COMPLETED, T3: S.COMPLETED}
    assert provider.max_active == 2

    events = await events_of(database, run_id)
    t3_start = seq(events, EventType.TASK_STARTED, T3)
    assert seq(events, EventType.TASK_COMPLETED, T1) < t3_start
    assert seq(events, EventType.TASK_COMPLETED, T2) < t3_start

    # The analyst saw both researchers' results through its task context.
    [analyst_request] = [r for r in provider.requests if r.purpose == "agent:analyst"]
    assert "Product A is the market leader." in analyst_request.messages[0].content
    assert "Product C targets professional users." in analyst_request.messages[0].content

    # All state is event-derived and reconstructable from the log alone.
    rebuilt = project(events)
    incremental = None
    for event in events:
        incremental = apply(incremental, event)
    async with database.session_factory() as session:
        assert await RunService(session).get_state(run_id) == rebuilt == incremental

    facts = [e for e in events if e.event_type is EventType.FACT_ADDED]
    assert len(facts) == len(rebuilt.facts) == 4
    for event in facts:
        fact = rebuilt.facts[event.payload.fact_id]  # type: ignore[attr-defined]
        assert (fact.agent_id, fact.task_id) == (event.agent_id, event.task_id)
        assert seq(events, EventType.TASK_STARTED, event.task_id) < event.sequence < seq(events, EventType.TASK_COMPLETED, event.task_id)  # type: ignore[arg-type]
    assert rebuilt.facts[f"{T1}.f1"].agent_id == "researcher"
    assert rebuilt.artifacts[f"{T3}.a1"].name == "comparison.md"
    assert rebuilt.tasks[T3].evidence[0].reference == T1
    assert rebuilt.tasks[T3].result_metadata["agent_type"] == "analyst"


async def test_agent_failure_blocks_dependents(database: Database) -> None:
    run_id = await new_run(database)
    provider = research_provider(errors={T1: LLMError("provider down")})
    await plan(database, provider, run_id)

    report = await asyncio.wait_for(scheduler(database, provider).run(run_id), TIMEOUT)

    assert report.task_statuses == {T1: S.FAILED, T2: S.COMPLETED, T3: S.BLOCKED}
    async with database.session_factory() as session:
        state = await RunService(session).get_state(run_id)
    assert state.tasks[T1].error == "llm_error: provider down"
    assert [r.purpose for r in provider.requests].count("agent:analyst") == 0


class RogueExecutor:
    """Tries to smuggle events that only NEXUS may write."""

    def __init__(self, events: list) -> None:  # type: ignore[type-arg]
        self._events = events

    async def execute(self, task: TaskState, context: TaskContext) -> TaskExecutionResult:
        return TaskExecutionResult(succeeded=True, summary="done", events=self._events)


@pytest.mark.parametrize(
    ("events", "match"),
    [
        ([RunCompleted(summary="I decided the run is over")], "disallowed event types ['RunCompleted']"),
        ([FactAdded(fact_id="dup", content="a"), FactAdded(fact_id="dup", content="b")], "result events rejected"),
    ],
    ids=["disallowed-type", "invalid-events"],
)
async def test_executor_cannot_write_arbitrary_events(database: Database, events: list, match: str) -> None:  # type: ignore[type-arg]
    run_id = await new_run(database)
    await plan(database, research_provider(), run_id)

    report = await asyncio.wait_for(Scheduler(database, RogueExecutor(events)).run(run_id), TIMEOUT)

    recorded = await events_of(database, run_id)
    assert EventType.RUN_COMPLETED not in {e.event_type for e in recorded}
    assert EventType.FACT_ADDED not in {e.event_type for e in recorded}
    assert set(report.task_statuses.values()) == {S.FAILED, S.BLOCKED}
    async with database.session_factory() as session:
        state = await RunService(session).get_state(run_id)
    assert match in (state.tasks[T1].error or "")
    assert state.status.value == "created"


# --- API -----------------------------------------------------------------------------------


@asynccontextmanager
async def client(settings: Settings, database: Database, provider: FakeLLMProvider | None) -> AsyncIterator[httpx.AsyncClient]:
    # Phase 3 scope: no tools, so agents answer with a single report (tools: test_tool_integration).
    app = create_app(settings, database=database, llm_provider=provider, tool_registry=ToolRegistry())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_api_goal_plan_inspect_schedule(settings: Settings, database: Database) -> None:
    async with client(settings, database, research_provider()) as api:
        run = (await api.post("/api/v1/runs", json={"goal": GOAL, "constraints": ["No subscriptions"]})).json()
        base = f"/api/v1/runs/{run['id']}"

        planned_response = await api.post(f"{base}/plan")
        assert planned_response.status_code == 201, planned_response.text
        body = planned_response.json()
        assert (body["provider"], body["model"]) == ("fake", "fake-model")
        assert [t["task_id"] for t in body["tasks"]] == [T1, T2, T3]

        tasks = (await api.get(f"{base}/tasks")).json()
        assert {t["task_id"]: t["status"] for t in tasks} == {T1: "ready", T2: "ready", T3: "pending"}

        again = await api.post(f"{base}/plan")
        assert again.status_code == 409 and again.json()["error"]["code"] == "plan_exists"

        scheduled = await api.post(f"{base}/schedule", json={"executor": "agent"})
        assert scheduled.status_code == 200, scheduled.text
        assert scheduled.json()["task_statuses"] == {T1: "completed", T2: "completed", T3: "completed"}

        state = (await api.get(f"{base}/state")).json()
        assert state["constraints"] == ["No subscriptions"]
        assert len(state["facts"]) == 4 and f"{T3}.a1" in state["artifacts"]


async def test_api_planning_errors(settings: Settings, database: Database) -> None:
    async with client(settings, database, None) as api:
        run_id = (await api.post("/api/v1/runs", json={"goal": GOAL})).json()["id"]
        response = await api.post(f"/api/v1/runs/{run_id}/plan")
        assert response.status_code == 503 and response.json()["error"]["code"] == "llm_not_configured"

    cyclic = {"tasks": [planned("a", dependencies=["b"]), planned("b", dependencies=["a"])]}
    async with client(settings, database, FakeLLMProvider({"planner": FakeReply(data=cyclic)})) as api:
        response = await api.post(f"/api/v1/runs/{run_id}/plan")
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "plan_rejected" and "dependency_cycle" in error["message"]

    async with client(settings, database, FakeLLMProvider({"planner": FakeReply(error=LLMError("down"))})) as api:
        response = await api.post(f"/api/v1/runs/{run_id}/plan")
        assert response.status_code == 502 and response.json()["error"]["code"] == "llm_error"
        events = (await api.get(f"/api/v1/runs/{run_id}/events")).json()
        assert [e["event_type"] for e in events] == ["RunCreated"]
