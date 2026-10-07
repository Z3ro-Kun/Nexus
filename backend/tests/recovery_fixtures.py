"""Helpers for Phase 5 tests: a deterministic failure-and-recovery scenario.

Nothing here calls a real LLM or the network. The replanner's replies are scripted with
FakeLLMProvider (purpose "replanner"); tool failures are injected with
FakeSearchBackend(failures=...), so they never depend on a real outage.

Scenario ("Research several candidate products and compare them."):

    research_source_a --\\
                         +--> compare_products
    research_source_b --/

research_source_a's web search fails (source A "unavailable"); the replanner proposes
research_source_c, which replaces it; compare_products then runs on B + C.
"""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from uuid import UUID

from app.agents.registry import AgentRegistry
from app.agents.runtime import AgentTaskExecutor
from app.events.base import Event
from app.llm.fake import FakeLLMProvider, FakeReply
from app.llm.schemas import LLMRequest
from app.orchestration.scheduler import ScheduleReport, Scheduler
from app.persistence.database import Database
from app.persistence.repositories import EventRepository
from app.recovery.manager import RecoveryManager
from app.recovery.policy import RecoveryPolicy
from app.services.planning import PlanningService
from app.services.runs import RunService
from app.state.models import RunState
from app.tools.errors import ToolUnavailableError
from app.tools.executor import ToolExecutor
from app.tools.fakes import FAKE_DOMAIN, FakeSearchBackend
from app.tools.policy import ToolPolicy
from tests.agent_fixtures import GOAL, planned
from tests.tool_fixtures import call_tool, fact, fake_registry, finish, turn_of

TIMEOUT = 10
A, B, C, COMPARE = "research_source_a", "research_source_b", "research_source_c", "compare_products"
QA, QB, QC = "source A products", "source B products", "source C products"


def url(query: str, n: int = 1) -> str:
    return f"https://{FAKE_DOMAIN}/{'-'.join(query.split()).lower()}/{n}"


PLAN: dict[str, Any] = {
    "tasks": [
        planned(A, description="Research candidate products using source A (web search)."),
        planned(B, description="Research candidate products using source B (web search)."),
        planned(
            COMPARE,
            agent_type="analyst",
            task_type="analysis",
            dependencies=[A, B],
            description="Compare every candidate product found and recommend the best fit.",
        ),
    ]
}


def researcher_steps(task_id: str, query: str, product: str) -> list[dict[str, Any]]:
    return [
        call_tool("web_search", query=query, max_results=2),
        finish(
            f"Found {product} via {query}",
            [fact(f"{product} is a candidate.", "tool_output", f"{task_id}.t1", url(query))],
        ),
    ]


COMPARE_STEPS = [
    finish(
        "Product C is the best fit.",
        [fact("Product C is recommended over product B.")],
        evidence=[
            {"source": "task_context", "reference": C, "note": "replacement research"},
            {"source": "task_context", "reference": B, "note": "source B research"},
        ],
    )
]

STEPS: dict[str, list[dict[str, Any]]] = {
    A: researcher_steps(A, QA, "Product A"),  # its search fails before it can finish
    B: researcher_steps(B, QB, "Product B"),
    C: researcher_steps(C, QC, "Product C"),
    COMPARE: COMPARE_STEPS,
}


def replan_task(
    task_id: str,
    *,
    replaces: str | None = None,
    agent_type: str = "researcher",
    task_type: str = "research",
    dependencies: list[str] | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    return planned(
        task_id,
        agent_type=agent_type,
        task_type=task_type,
        dependencies=dependencies,
        description=description or f"Research candidate products for {task_id.replace('_', ' ')}.",
        replaces=replaces,
    )


def replan(summary: str, *tasks: dict[str, Any]) -> dict[str, Any]:
    return {"strategy_summary": summary, "tasks": list(tasks)}


SOURCE_C_REPLAN = replan(
    "Source A is unavailable; research the same products using source C instead.",
    replan_task(
        C,
        replaces=A,
        description="Research candidate products using source C (web search), since source A is unavailable.",
    ),
)


def in_order(*replies: Mapping[str, Any] | Exception) -> Callable[[LLMRequest], FakeReply]:
    """Replanner reply source: the n-th call gets the n-th reply (the last one repeats)."""
    calls = {"n": 0}

    def reply(_: LLMRequest) -> FakeReply:
        item = replies[min(calls["n"], len(replies) - 1)]
        calls["n"] += 1
        if isinstance(item, Exception):
            return FakeReply(error=item)  # type: ignore[arg-type]
        return FakeReply(data=item)

    return reply


def agent_steps(
    steps: Mapping[str, list[dict[str, Any]]],
    *,
    hang: Sequence[str] = (),
    gates: Mapping[str, asyncio.Event] | None = None,
) -> Callable[[LLMRequest], FakeReply]:
    """Tool-loop replies by task id. Tasks in `hang` never answer (to force an agent
    timeout); `gates` hold a task's first LLM call until set."""
    never = asyncio.Event()

    def reply(request: LLMRequest) -> FakeReply:
        task_id = request.metadata["task_id"]
        if task_id in hang:
            return FakeReply(data=steps[task_id][0], wait_for=never)
        script = steps[task_id]
        turn = turn_of(request)
        gate = (gates or {}).get(task_id) if turn == 0 else None
        return FakeReply(data=script[min(turn, len(script) - 1)], wait_for=gate)

    return reply


def provider(
    replanner: Any,
    steps: Mapping[str, list[dict[str, Any]]] = STEPS,
    plan: Mapping[str, Any] = PLAN,
    **step_options: Any,
) -> FakeLLMProvider:
    agents = agent_steps(steps, **step_options)
    replies: dict[str, Any] = {"planner": FakeReply(data=plan), "agent:researcher": agents, "agent:analyst": agents}
    if replanner is not None:
        replies["replanner"] = replanner
    return FakeLLMProvider(replies)


def search_backend(fail: Sequence[str] = (QA,), gates: Mapping[str, asyncio.Event] | None = None) -> FakeSearchBackend:
    return FakeSearchBackend(
        failures={q: ToolUnavailableError(f"search backend for {q!r} is unavailable (fake outage)") for q in fail},
        gates=gates,
    )


class Harness:
    """Planner -> scheduler (+ recovery) over a real database, with fake LLM and tools."""

    def __init__(
        self,
        database: Database,
        llm: FakeLLMProvider,
        search: FakeSearchBackend,
        *,
        max_replans: int = 2,
        max_new_tasks: int = 10,
        agent_timeout: float = TIMEOUT,
        recovery: bool = True,
    ) -> None:
        self.database = database
        self.llm = llm
        self.search = search
        self.registry = AgentRegistry(
            llm,
            max_tokens=4000,
            tool_executor=ToolExecutor(fake_registry(search), ToolPolicy()),
            max_tool_calls=3,
        )
        self.manager = (
            RecoveryManager(
                database,
                self.registry.replanner(),
                RecoveryPolicy(max_replans),
                max_new_tasks=max_new_tasks,
                timeout_seconds=TIMEOUT,
            )
            if recovery
            else None
        )
        self.scheduler = Scheduler(
            database, AgentTaskExecutor(self.registry, timeout_seconds=agent_timeout), self.manager
        )

    async def planned_run(self) -> UUID:
        async with self.database.session_factory() as session:
            run_id = (await RunService(session).create_run(GOAL)).id
        async with self.database.session_factory() as session:
            await PlanningService(
                session, self.registry.planner(), max_tasks=10, timeout_seconds=TIMEOUT
            ).plan_run(run_id)
        return run_id

    async def schedule(self, run_id: UUID) -> ScheduleReport:
        return await asyncio.wait_for(self.scheduler.run(run_id), TIMEOUT)

    async def events(self, run_id: UUID) -> list[Event]:
        async with self.database.session_factory() as session:
            return await EventRepository(session).list_for_run(run_id)

    async def state(self, run_id: UUID) -> RunState:
        async with self.database.session_factory() as session:
            return await RunService(session).get_state(run_id)

    def replanner_calls(self) -> list[LLMRequest]:
        return [r for r in self.llm.requests if r.purpose == "replanner"]


def timeline(events: Sequence[Event]) -> list[tuple[str, str | None]]:
    """(event type, subject task id) for every event, in order."""
    out = []
    for e in events:
        payload = e.payload
        subject = getattr(payload, "task_id", None) or getattr(payload, "failed_task_id", None) or e.task_id
        out.append((e.event_type.value, subject))
    return out
