"""Goal -> planner -> validated plan -> TaskCreated events. Never executes tasks.

Scheduling is a separate operation, so a plan can be inspected before anything runs.

Phase 9: with a `checkpoint` spec (built by trusted code, never by the planner), the plan
and a final verification checkpoint over all planned work are appended in the same
atomic append, so a run is never planned without its verification boundary.

Phase 11: if the planner decides the objective needs clarification, NO task (and no
checkpoint) is created. ClarificationRequested is appended instead, which ends the run in
status needs_clarification, so the scheduler, agents, tools, recovery and verification
never run for it.
"""

import asyncio
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.planner import ClarificationRequest, PlannerAgent
from app.core.exceptions import LLMTimeoutError, PlanAlreadyExistsError, RunNotActiveError
from app.events.base import NewEvent
from app.events.types import ClarificationRequested, VerificationSpec
from app.models.run import TERMINAL_RUN_STATUSES
from app.services.runs import RunService
from app.state.models import TaskState

PLANNER_AGENT_ID = "planner"


class PlanResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    tasks: list[TaskState]
    provider: str
    model: str
    # Phase 11: set (and `tasks` empty) when the planner asked for clarification.
    clarification: ClarificationRequest | None = None


class PlanningService:
    def __init__(
        self,
        session: AsyncSession,
        planner: PlannerAgent,
        *,
        max_tasks: int,
        timeout_seconds: float,
    ) -> None:
        self._runs = RunService(session)
        self._planner = planner
        self._max_tasks = max_tasks
        self._timeout = timeout_seconds

    async def plan_run(
        self, run_id: UUID, *, checkpoint: VerificationSpec | None = None, checkpoint_id: str = "verify_objective"
    ) -> PlanResult:
        state = await self._runs.get_state(run_id)
        if state.status in TERMINAL_RUN_STATUSES:
            raise RunNotActiveError(f"run {run_id} is {state.status.value}")
        if state.tasks:
            # One plan per run; replanning is not implemented.
            raise PlanAlreadyExistsError(f"run {run_id} already has {len(state.tasks)} tasks")

        try:
            plan = await asyncio.wait_for(
                self._planner.plan(state.goal, state.constraints, max_tasks=self._max_tasks),
                self._timeout,
            )
        except asyncio.TimeoutError:
            raise LLMTimeoutError(f"planner did not finish within {self._timeout:g}s") from None

        # expected_sequence: if anything was appended while the planner ran (for example a
        # concurrent plan request), this plan is rejected with a sequence conflict.
        if plan.clarification is not None:
            await self._runs.append_events(
                run_id,
                [NewEvent(payload=ClarificationRequested(
                    reason=plan.clarification.reason,
                    question=plan.clarification.question,
                    missing=list(plan.clarification.missing),
                    provider=plan.provider[:100],
                    model=plan.model[:200],
                ), agent_id=PLANNER_AGENT_ID)],
                expected_sequence=state.last_sequence,
            )
            return PlanResult(tasks=[], provider=plan.provider, model=plan.model, clarification=plan.clarification)
        payloads = plan.to_task_payloads()
        if checkpoint is not None:
            from app.verification.checkpoint import checkpoint_task

            payloads.append(
                checkpoint_task(state, task_id=checkpoint_id, spec=checkpoint, dependencies=[p.task_id for p in payloads])
            )
        tasks = await self._runs.create_tasks(
            run_id,
            payloads,
            agent_id=PLANNER_AGENT_ID,
            expected_sequence=state.last_sequence,
        )
        return PlanResult(tasks=tasks, provider=plan.provider, model=plan.model)
