"""The orchestrator: one entry point that carries a run from its objective to verified
completion, by coordinating the existing components. It owns no execution, recovery,
conflict, policy or verification logic of its own.

    Orchestrator.run(run_id)
      1. plan once: if the run has no tasks, the planner proposes a plan; the plan and a
         final verification checkpoint over all planned work (spec built by trusted code,
         never by the model) are appended atomically (PlanningService)
      2. ensure a checkpoint exists for runs whose tasks were created otherwise
      3. scheduler passes (the existing Scheduler, with recovery, conflicts, verification,
         the policy gate and completion), repeated while a pass makes progress (appends
         events), at most `max_passes` times
      4. stop at: completed, failed, waiting for approval, or blocked (a pass that appends
         nothing). No busy loop: a pass that changes nothing ends the call.
      Phase 11: if the planner asks for clarification, step 1 records it instead of a plan;
      the run is then needs_clarification (terminal), so steps 2-3 never run: no task,
      checkpoint, scheduler pass, agent, tool, recovery or verification.
      <- OrchestrationResult (the derived phase + the final result)

Resuming is calling it again: e.g. after a human approved an action, the next call's
scheduler pass executes it, then verification and completion follow.

Idempotency and concurrency come from the existing event guarantees: one plan per run
(planning requires an empty task graph and appends with expected_sequence), a task
starts at most once (TaskStarted only from READY), one decision / approval per action,
one verdict per checkpoint, and RunCompleted only once (the run is terminal after it).
"""

import logging
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.agents.planner import PlannerAgent
from app.core.exceptions import (
    LLMNotConfiguredError,
    PlanAlreadyExistsError,
    RunNotActiveError,
    SequenceConflictError,
    TaskGraphError,
)
from app.events.types import VerificationSpec
from app.models.run import TERMINAL_RUN_STATUSES
from app.orchestration.result import FinalResult, RunPhase, build_final_result
from app.orchestration.scheduler import ScheduleReport, Scheduler
from app.persistence.database import Database
from app.services.planning import PlanningService
from app.services.runs import RunService
from app.state.models import RunState
from app.verification.checkpoint import checkpoint_task, default_scope

logger = logging.getLogger(__name__)

# Not a valid planner task id (planner ids have no dots), so it can never collide.
CHECKPOINT_ID = "verify.objective"
ORCHESTRATOR_AGENT_ID = "orchestrator"


def objective_checkpoint(state: RunState, *, semantic: bool) -> VerificationSpec:
    """The final checkpoint's requirements, built by trusted code from the run itself.
    The deterministic checks (work completed, provenance, conflicts, actions authorized
    and executed) always apply; `semantic` adds the LLM judgement of objective and
    constraints. Nothing here comes from model output."""
    return VerificationSpec(objective=state.goal[:2000], semantic=semantic)


class OrchestrationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: UUID
    phase: RunPhase
    planned: bool  # this call created the plan
    passes: int  # scheduler passes made by this call
    started: list[str]
    completed: list[str]
    failed: list[str]
    replanned: list[str]
    approvals_pending: list[str]
    result: FinalResult


class Orchestrator:
    def __init__(
        self,
        database: Database,
        scheduler: Scheduler,
        planner: PlannerAgent | None,
        *,
        max_tasks: int,
        timeout_seconds: float,
        semantic_verification: bool = False,
        max_passes: int = 10,
    ) -> None:
        self._database = database
        self._scheduler = scheduler
        self._planner = planner
        self._max_tasks = max_tasks
        self._timeout = timeout_seconds
        self._semantic = semantic_verification
        self._max_passes = max_passes

    async def run(self, run_id: UUID) -> OrchestrationResult:
        state = await self._load(run_id)
        planned = False
        if state.status not in TERMINAL_RUN_STATUSES and not state.tasks:
            planned = await self._plan(state)
            state = await self._load(run_id)
        if state.status not in TERMINAL_RUN_STATUSES and state.tasks and not state.verifications:
            await self._ensure_checkpoint(run_id)
            state = await self._load(run_id)

        reports: list[ScheduleReport] = []
        for _ in range(self._max_passes):
            if state.status in TERMINAL_RUN_STATUSES:
                break
            before = state.last_sequence
            try:
                reports.append(await self._scheduler.run(run_id))
            except RunNotActiveError:
                state = await self._load(run_id)  # another orchestrator finished the run
                break
            state = await self._load(run_id)
            if state.last_sequence == before:
                break  # no progress: waiting for approval, blocked, or nothing left

        result = build_final_result(state)
        logger.info("orchestration of run %s: %s after %d passes", run_id, result.phase.value, len(reports))
        return OrchestrationResult(
            run_id=run_id,
            phase=result.phase,
            planned=planned,
            passes=len(reports),
            started=[t for r in reports for t in r.started],
            completed=[t for r in reports for t in r.completed],
            failed=[t for r in reports for t in r.failed],
            replanned=[t for r in reports for t in r.replanned],
            approvals_pending=sorted(a.approval_id for a in state.approvals.values() if a.status.value == "pending"),
            result=result,
        )

    async def _plan(self, state: RunState) -> bool:
        if self._planner is None:
            raise LLMNotConfiguredError("planning a run needs a configured LLM provider")
        try:
            async with self._database.session_factory() as session:
                await PlanningService(
                    session, self._planner, max_tasks=self._max_tasks, timeout_seconds=self._timeout
                ).plan_run(state.run_id, checkpoint=objective_checkpoint(state, semantic=self._semantic), checkpoint_id=CHECKPOINT_ID)
        except (PlanAlreadyExistsError, SequenceConflictError):
            logger.info("run %s was planned concurrently; using that plan", state.run_id)
            return False
        return True

    async def _ensure_checkpoint(self, run_id: UUID) -> None:
        """A checkpoint over the current work, for runs planned without one."""
        async with self._database.session_factory() as session:
            service = RunService(session)
            state = await service.get_state(run_id)
            if state.verifications or not default_scope(state):
                return
            task = checkpoint_task(state, task_id=CHECKPOINT_ID, spec=objective_checkpoint(state, semantic=self._semantic))
            try:
                await service.create_tasks(run_id, [task], agent_id=ORCHESTRATOR_AGENT_ID, expected_sequence=state.last_sequence)
            except (SequenceConflictError, TaskGraphError) as exc:
                # A concurrent call created it, or the work cannot be verified (e.g. it failed).
                logger.info("checkpoint for run %s not created: %s", run_id, exc)

    async def _load(self, run_id: UUID) -> RunState:
        async with self._database.session_factory() as session:
            return await RunService(session).get_state(run_id)
