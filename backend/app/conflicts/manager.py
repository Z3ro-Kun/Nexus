"""ConflictManager: runs after each completed task; the only writer of conflict events.

    Scheduler records TaskCompleted
        -> ConflictManager.after_task_completed(run_id, task_id)
            1. if the task resolves an OPEN conflict (directly, or as a Phase 5 replacement
               of the resolution task): append ConflictResolved / ConflictUnresolved,
               decided by `app.conflicts.resolution.evaluate` (deterministic);
            2. detect new conflicts (`app.state.conflict_detector`, deterministic) and
               append, per conflict, ConflictDetected + TaskCreated(resolution task) in
               one atomic append, while the run's resolution budget allows (conflicts
               beyond it are still recorded, OPEN, without a task).
        <- ConflictOutcome

It never calls an LLM, never executes tasks and never changes or deletes facts. A
failed resolution task is not special: the scheduler hands it to Phase 5 recovery like
any other failure; a replacement inherits the conflict through `replaces`. While no
resolver completes, the conflict stays OPEN.
"""

import logging
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.conflicts.resolution import evaluate, resolution_task
from app.core.exceptions import SchedulerContentionError, SequenceConflictError
from app.events.base import NewEvent
from app.events.types import ConflictDetected, ConflictResolved
from app.models.run import TERMINAL_RUN_STATUSES
from app.persistence.database import Database
from app.services.runs import RunService
from app.state.conflict_detector import detect_conflicts, resolution_origin
from app.state.models import ConflictStatus, RunState

logger = logging.getLogger(__name__)

DETECTOR_AGENT_ID = "conflict_detector"
RESOLVER_AGENT_ID = "conflict_resolver"
MAX_APPEND_ATTEMPTS = 50


class ConflictOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    detected: tuple[str, ...] = ()
    resolution_tasks: tuple[str, ...] = ()
    resolved: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()


class ConflictManager:
    def __init__(self, database: Database, *, max_resolution_tasks: int) -> None:
        self._database = database
        self._max_tasks = max_resolution_tasks

    async def after_task_completed(self, run_id: UUID, task_id: str | None) -> ConflictOutcome:
        """`task_id` None: only detect (e.g. facts appended outside the scheduler)."""
        resolved: tuple[str, ...] = ()
        unresolved: tuple[str, ...] = ()
        if task_id is not None:
            verdict = await self._conclude(run_id, task_id)
            if isinstance(verdict, ConflictResolved):
                resolved = (verdict.conflict_id,)
            elif verdict is not None:
                unresolved = (verdict.conflict_id,)
        detected, tasks = await self._detect(run_id)
        return ConflictOutcome(detected=detected, resolution_tasks=tasks, resolved=resolved, unresolved=unresolved)

    async def _conclude(self, run_id: UUID, task_id: str):  # type: ignore[no-untyped-def]
        for _ in range(MAX_APPEND_ATTEMPTS):
            async with self._database.session_factory() as session:
                service = RunService(session)
                state = await service.get_state(run_id)
                conflict_id = resolution_origin(state, task_id)
                conflict = state.conflicts.get(conflict_id) if conflict_id else None
                if conflict is None or conflict.status is not ConflictStatus.OPEN or state.status in TERMINAL_RUN_STATUSES:
                    return None
                verdict = evaluate(state, conflict, task_id)
                try:
                    await service.append_events(
                        run_id,
                        [NewEvent(payload=verdict, agent_id=RESOLVER_AGENT_ID, task_id=task_id)],
                        expected_sequence=state.last_sequence,
                    )
                except SequenceConflictError:
                    continue
                logger.info("conflict %s: %s by task %r", conflict.conflict_id, verdict.event_type.value, task_id)
                return verdict
        raise SchedulerContentionError(f"could not record conflict verdict after {MAX_APPEND_ATTEMPTS} attempts")

    async def _detect(self, run_id: UUID) -> tuple[tuple[str, ...], tuple[str, ...]]:
        for _ in range(MAX_APPEND_ATTEMPTS):
            async with self._database.session_factory() as session:
                service = RunService(session)
                state = await service.get_state(run_id)
                if state.status in TERMINAL_RUN_STATUSES:
                    return (), ()
                drafts, detected, tasks = self._drafts(state)
                if not drafts:
                    return (), ()
                try:
                    await service.append_events(run_id, drafts, expected_sequence=state.last_sequence)
                except SequenceConflictError:
                    continue
                logger.info("conflicts detected: %s; resolution tasks: %s", detected, tasks)
                return detected, tasks
        raise SchedulerContentionError(f"could not record conflicts after {MAX_APPEND_ATTEMPTS} attempts")

    def _drafts(self, state: RunState) -> tuple[list[NewEvent], tuple[str, ...], tuple[str, ...]]:
        budget = self._max_tasks - sum(1 for t in state.tasks.values() if t.conflict_id is not None)
        drafts: list[NewEvent] = []
        detected: list[str] = []
        tasks: list[str] = []
        for candidate in detect_conflicts(state):
            drafts.append(
                NewEvent(
                    payload=ConflictDetected(
                        conflict_id=candidate.conflict_id,
                        fact_ids=list(candidate.fact_ids),
                        conflict_type=candidate.conflict_type,
                        fact_key=candidate.fact_key,
                        fingerprint=candidate.fingerprint,
                        reason=candidate.reason,
                    ),
                    agent_id=DETECTOR_AGENT_ID,
                )
            )
            detected.append(candidate.conflict_id)
            if budget > 0:
                task = resolution_task(state, candidate)
                drafts.append(NewEvent(payload=task, agent_id=DETECTOR_AGENT_ID))
                tasks.append(task.task_id)
                budget -= 1
            else:
                logger.warning("conflict %s recorded without a resolution task (budget spent)", candidate.conflict_id)
        return drafts, tuple(detected), tuple(tasks)
