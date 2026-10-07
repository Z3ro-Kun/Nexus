"""Helpers for Phase 9 tests: one objective through the whole pipeline, deterministically.

Nothing calls a real LLM or the network. The planner, agents, replanner and semantic
verifier are scripted with FakeLLMProvider (by purpose and task id); prices come from
FakeSearchBackend; the order is a FakeSideEffectTool whose executions are counted.

Scenario (objective GOAL):

    research_a (search source A) --\\
                                    +--> compare (analyst; recommendation artifact) --> [buy_laptop: place_order]
    research_b (search source B) --/
                                                       verify.objective (checkpoint created by NEXUS, not the planner)
"""

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from uuid import UUID

from app.agents.registry import AgentRegistry
from app.agents.runtime import AgentTaskExecutor
from app.conflicts.manager import ConflictManager
from app.events.base import Event
from app.events.types import ActionCategory
from app.llm.fake import FakeLLMProvider, FakeReply
from app.llm.schemas import LLMRequest
from app.orchestration.orchestrator import CHECKPOINT_ID, OrchestrationResult, Orchestrator
from app.orchestration.scheduler import Scheduler
from app.persistence.database import Database
from app.persistence.repositories import EventRepository
from app.policy.actions import ActionTaskExecutor
from app.policy.completion import RunCompletion
from app.policy.engine import PolicyEngine
from app.policy.manager import ApprovalManager, PolicyManager
from app.recovery.manager import RecoveryManager
from app.recovery.policy import RecoveryPolicy
from app.services.runs import RunService
from app.state.models import RunState
from app.tools.executor import ToolExecutor
from app.tools.fakes import FakeSideEffectTool
from app.tools.policy import ToolPolicy
from app.verification.manager import VerificationManager
from app.verification.semantic import LLMSemanticVerifier
from app.verification.verifier import Verifier
from tests.agent_fixtures import planned
from tests.conflict_fixtures import COMPARE, QA, QB, QC, RA, RB, agent_reply, researcher, resolver_finds
from tests.recovery_fixtures import search_backend
from tests.tool_fixtures import fake_registry, finish

TIMEOUT = 10
GOAL = "Find a laptop under 100000 INR, compare current prices from two sources, and recommend one."
BUY, ORDER = "buy_laptop", "place_order"
APPROVAL = f"{BUY}.approval"
VERIFY = CHECKPOINT_ID

RECOMMENDATION = {"product": "Product X", "price": 94999, "decision": "buy"}


def compare_steps(summary: str = "Product X is the best fit under budget.") -> list[dict[str, Any]]:
    return [finish(summary, [], artifacts=[{"name": "recommendation", "media_type": "application/json",
                                             "content": json.dumps(RECOMMENDATION)}])]


def plan(*, with_action: bool = False) -> dict[str, Any]:
    tasks = [
        planned(RA, description="Find Product X's current price using source A (web search)."),
        planned(RB, description="Find Product X's current price using source B (web search)."),
        planned(COMPARE, agent_type="analyst", task_type="analysis", dependencies=[RA, RB],
                description="Compare the prices found and recommend one laptop under budget."),
    ]
    actions = [{
        "id": BUY, "title": "Order the recommended laptop",
        "description": "Order one unit of the recommended laptop (Product X).",
        "tool_name": ORDER, "arguments": [{"name": "item", "value": "Product X"}, {"name": "quantity", "value": 1}],
        "dependencies": [COMPARE],
    }] if with_action else []
    return {"tasks": tasks, "actions": actions}


def steps(a: Any = 94999, b: Any = 94999, **extra: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {RA: researcher(RA, QA, a), RB: researcher(RB, QB, b), COMPARE: compare_steps(), **extra}


def verifier_says(verdict: str = "pass", evidence: Sequence[str] = (f"{RA}.f1",), explanation: str = "Supported.") -> dict[str, Any]:
    return {"objective": {"verdict": verdict, "evidence": list(evidence), "explanation": explanation},
            "constraints": [], "summary": "Checked against the evidence."}


def in_order(*replies: Any) -> Callable[[LLMRequest], FakeReply]:
    calls = {"n": 0}

    def reply(_: LLMRequest) -> FakeReply:
        item = replies[min(calls["n"], len(replies) - 1)]
        calls["n"] += 1
        return item if isinstance(item, FakeReply) else FakeReply(data=item)

    return reply


def llm(
    agent_steps: Mapping[str, list[dict[str, Any]]] | None = None,
    *,
    planner: Mapping[str, Any] | None = None,
    resolvers: Any = None,
    replanner: Any = None,
    verifier: Any = None,
    gates: Mapping[str, asyncio.Event] | None = None,
) -> FakeLLMProvider:
    agents = agent_reply(agent_steps or steps(), resolvers if resolvers is not None else resolver_finds(QC, 96999), gates)
    replies: dict[str, Any] = {"planner": FakeReply(data=planner or plan()), "agent:researcher": agents,
                               "agent:analyst": agents, "agent:specialist": agents}
    if replanner is not None:
        replies["replanner"] = replanner
    if verifier is not None:
        replies["verifier"] = verifier if callable(verifier) else FakeReply(data=verifier)
    return FakeLLMProvider(replies)


class Pipeline:
    """The real Phase 3-8 components behind one Orchestrator, over a real database."""

    def __init__(
        self,
        database: Database,
        provider: FakeLLMProvider,
        *,
        search_failures: Sequence[str] = (),
        policy: PolicyEngine | None = None,
        semantic: bool = False,
        max_replans: int = 2,
        recovery: bool = True,
    ) -> None:
        self.database = database
        self.llm = provider
        self.order = FakeSideEffectTool(ORDER, ActionCategory.IRREVERSIBLE)
        tools = fake_registry(search_backend(fail=list(search_failures)))
        tools.register(self.order)
        engine = policy or PolicyEngine()
        tool_executor = ToolExecutor(tools, ToolPolicy(), engine)
        self.registry = AgentRegistry(provider, max_tokens=4000, tool_executor=tool_executor, max_tool_calls=3)
        self.recovery = (
            RecoveryManager(database, self.registry.replanner(), RecoveryPolicy(max_replans), max_new_tasks=10, timeout_seconds=TIMEOUT)
            if recovery else None
        )
        self.semantic = semantic
        self._tool_executor = tool_executor
        self.approvals = ApprovalManager(database)

    def orchestrator(self) -> Orchestrator:
        scheduler = Scheduler(
            self.database,
            AgentTaskExecutor(self.registry, timeout_seconds=TIMEOUT),
            self.recovery,
            ConflictManager(self.database, max_resolution_tasks=5),
            VerificationManager(self.database, Verifier(LLMSemanticVerifier(self.llm, max_tokens=4000)), timeout_seconds=TIMEOUT),
            PolicyManager(self.database, self._tool_executor.engine, ActionTaskExecutor(self._tool_executor)),
            RunCompletion(self.database),
        )
        return Orchestrator(self.database, scheduler, self.registry.planner(), max_tasks=10, timeout_seconds=TIMEOUT,
                            semantic_verification=self.semantic)

    async def create(self, goal: str = GOAL, constraints: Sequence[str] = ()) -> UUID:
        async with self.database.session_factory() as session:
            return (await RunService(session).create_run(goal, constraints)).id

    async def execute(self, run_id: UUID) -> OrchestrationResult:
        return await asyncio.wait_for(self.orchestrator().run(run_id), TIMEOUT * 3)

    async def state(self, run_id: UUID) -> RunState:
        async with self.database.session_factory() as session:
            return await RunService(session).get_state(run_id)

    async def events(self, run_id: UUID) -> list[Event]:
        async with self.database.session_factory() as session:
            return await EventRepository(session).list_for_run(run_id)

    def calls(self, purpose: str) -> list[LLMRequest]:
        return [r for r in self.llm.requests if r.purpose == purpose]
