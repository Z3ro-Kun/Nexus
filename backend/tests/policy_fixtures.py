"""Helpers for Phase 8 tests: fake side-effect tools behind the policy gate.

No real LLM, no network. FakeSideEffectTool counts its real executions, so tests can
prove the gate sits before the side effect. Agents and the replanner, where used, are
scripted with FakeLLMProvider.
"""

import asyncio
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from app.agents.registry import AgentRegistry
from app.agents.runtime import AgentTaskExecutor
from app.conflicts.manager import ConflictManager
from app.events.base import Event
from app.events.types import ActionCategory, ActionSpec, PolicyOutcome, TaskCreated, VerificationSpec
from app.llm.fake import FakeLLMProvider
from app.orchestration.scheduler import ScheduleReport, Scheduler
from app.persistence.database import Database
from app.persistence.repositories import EventRepository
from app.policy.actions import ActionTaskExecutor
from app.policy.completion import RunCompletion
from app.policy.engine import PolicyConfig, PolicyEngine
from app.policy.manager import ApprovalManager, PolicyManager
from app.recovery.manager import RecoveryManager
from app.recovery.policy import RecoveryPolicy
from app.services.runs import RunService
from app.state.models import RunState
from app.tools.executor import ToolExecutor
from app.tools.fakes import FakeSearchBackend, FakeSideEffectTool
from app.tools.policy import ToolPolicy
from app.verification.checkpoint import checkpoint_task
from app.verification.manager import VerificationManager
from app.verification.verifier import Verifier
from tests.tool_fixtures import fake_registry

TIMEOUT = 10
ORDER, DRAFT = "place_order", "save_draft"


def action(task_id: str, tool: str = ORDER, *, item: str = "laptop", quantity: int = 1, deps: Sequence[str] = (), **kw: Any) -> TaskCreated:
    return TaskCreated(
        task_id=task_id, title=f"Action {task_id}", agent_type="action_executor", task_type="action",
        dependencies=list(deps),
        action=ActionSpec(tool_name=tool, arguments={"item": item, "quantity": quantity}, intent=f"{tool} {quantity} x {item}"),
        **kw,
    )


def work(task_id: str, *deps: str) -> TaskCreated:
    return TaskCreated(task_id=task_id, title=task_id, dependencies=list(deps))


class Tools:
    """A fake registry with an irreversible and a reversible side-effect tool."""

    def __init__(self) -> None:
        self.order = FakeSideEffectTool(ORDER, ActionCategory.IRREVERSIBLE)
        self.draft = FakeSideEffectTool(DRAFT, ActionCategory.REVERSIBLE_WRITE)
        self.registry = fake_registry(FakeSearchBackend())
        self.registry.register(self.order)
        self.registry.register(self.draft)


def engine(**outcomes: str) -> PolicyEngine:
    denied = outcomes.pop("denied_tools", ())
    approval = outcomes.pop("approval_tools", ())
    return PolicyEngine(PolicyConfig(
        outcomes={ActionCategory(k): PolicyOutcome(v) for k, v in outcomes.items()},
        denied_tools=frozenset(denied), approval_tools=frozenset(approval),  # type: ignore[arg-type]
    ))


class PolicyHarness:
    """Scheduler with the policy gate, approvals, verification, recovery and completion,
    over a real database. Agent tasks use the scripted executor unless an LLM is given."""

    def __init__(
        self,
        database: Database,
        *,
        policy_engine: PolicyEngine | None = None,
        llm: FakeLLMProvider | None = None,
        recovery: bool = True,
        max_replans: int = 2,
        tool_permissions: dict[str, Any] | None = None,
    ) -> None:
        self.database = database
        self.tools = Tools()
        self.engine = policy_engine or PolicyEngine()
        tool_policy = ToolPolicy(tool_permissions) if tool_permissions is not None else ToolPolicy()
        self.tool_executor = ToolExecutor(self.tools.registry, tool_policy, self.engine)
        self.llm = llm or FakeLLMProvider({})
        self.uses_agents = llm is not None
        self.registry = AgentRegistry(self.llm, max_tokens=4000, tool_executor=self.tool_executor, max_tool_calls=3)
        self.recovery = (
            RecoveryManager(database, self.registry.replanner(), RecoveryPolicy(max_replans), max_new_tasks=10, timeout_seconds=TIMEOUT)
            if recovery else None
        )
        self.policy = PolicyManager(database, self.engine, ActionTaskExecutor(self.tool_executor))
        self.approvals = ApprovalManager(database)

    def scheduler(self, executor: Any = None) -> Scheduler:
        from app.orchestration.task_executor import ScriptedTaskExecutor

        return Scheduler(
            self.database,
            executor or (AgentTaskExecutor(self.registry, timeout_seconds=TIMEOUT) if self.uses_agents else ScriptedTaskExecutor()),
            self.recovery,
            ConflictManager(self.database, max_resolution_tasks=5),
            VerificationManager(self.database, Verifier(), timeout_seconds=TIMEOUT),
            self.policy,
            RunCompletion(self.database),
        )

    async def run(self, *tasks: TaskCreated, goal: str = "Buy a laptop within budget.") -> UUID:
        async with self.database.session_factory() as session:
            service = RunService(session)
            run_id = (await service.create_run(goal)).id
            if tasks:
                await service.create_tasks(run_id, list(tasks))
        return run_id

    async def add(self, run_id: UUID, *tasks: TaskCreated) -> None:
        async with self.database.session_factory() as session:
            await RunService(session).create_tasks(run_id, list(tasks))

    async def checkpoint(self, run_id: UUID, spec: VerificationSpec, *, task_id: str = "verify", dependencies: Sequence[str] | None = None) -> None:
        async with self.database.session_factory() as session:
            service = RunService(session)
            state = await service.get_state(run_id)
            await service.create_tasks(run_id, [checkpoint_task(state, task_id=task_id, spec=spec, dependencies=dependencies)])

    async def schedule(self, run_id: UUID, executor: Any = None) -> ScheduleReport:
        return await asyncio.wait_for(self.scheduler(executor).run(run_id), TIMEOUT)

    async def approve(self, run_id: UUID, approval_id: str, actor: str = "alice") -> Any:
        return await self.approvals.decide(run_id, approval_id, granted=True, actor=actor, reason="ok")

    async def reject(self, run_id: UUID, approval_id: str, reason: str = "too expensive") -> Any:
        return await self.approvals.decide(run_id, approval_id, granted=False, actor="alice", reason=reason)

    async def state(self, run_id: UUID) -> RunState:
        async with self.database.session_factory() as session:
            return await RunService(session).get_state(run_id)

    async def events(self, run_id: UUID) -> list[Event]:
        async with self.database.session_factory() as session:
            return await EventRepository(session).list_for_run(run_id)
