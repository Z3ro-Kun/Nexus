"""Deterministic scheduler: decides when each task may run and records the lifecycle.

One `Scheduler.run(run_id)` call loops:

1. project the run's state from its events and build the task graph;
2. claim every runnable (READY) task by appending `TaskStarted`;
3. start each claimed task on the executor as a separate asyncio task, so independent
   tasks run concurrently;
4. wait until one of its executions finishes, record `TaskCompleted` or `TaskFailed`,
   and go back to step 1.

It stops when it has nothing running and nothing is runnable. All task state lives in the
event log; the loop's only memory is the set of executions it has in flight.

Failures and recovery (Phase 5)
-------------------------------
A failed outcome is classified deterministically (`app.recovery.classifier`) and recorded
as TaskFailed with its failure type, error type and tool call id; the message is
redacted and truncated. If the scheduler was given a failure handler (the
`RecoveryManager`), it is then called with the failed task. The scheduler does not
decide anything about recovery; it only acts on the outcome:

- REPLAN: new tasks were created; the next loop iteration sees them (and any dependents
  they unblocked) as runnable;
- PROPAGATE: nothing more happens (dependents stay BLOCKED, as in Phase 2);
- FAIL_RUN: no further tasks are started; running ones are drained and recorded; then
  RunFailed is appended.

The scheduler never calls an LLM itself and never emits RunCompleted.

Conflicts (Phase 6)
-------------------
If given a conflict handler (the `ConflictManager`), the scheduler calls it once when
it starts (to pick up facts recorded outside it) and after every recorded
TaskCompleted. The handler records conflict verdicts and new conflicts with their
resolution tasks; the next loop iteration starts those tasks like any other.

Verification (Phase 7)
----------------------
A verification checkpoint (a task with a VerificationSpec) is never given to the task
executor. It is claimed like any task (TaskStarted, so it runs at most once) and run by
the verification handler (the `VerificationManager`), which records its verdict and the
task's outcome itself. The scheduler only decides *when*:
- not before its dependencies have completed (the task graph's READY rule);
- not while a relevant conflict's resolution is still pending (`deferral`);
- not at all without a verification handler (it then stays READY).
A failed checkpoint is handed to the failure handler like any failed task.

Policy gate and completion (Phase 8)
------------------------------------
An action task (a predeclared tool call) is never given to the task executor either.
When it is READY, the scheduler asks the policy handler (`PolicyManager.gate`), which
records the deterministic decision (and an approval request) before anything runs:
- allowed / approved: claim, then execute exactly the action (`PolicyManager.execute`);
- awaiting approval: leave it READY (nothing executes; `approvals_pending`);
- denied / rejected: claim and record TaskFailed (action_denied / approval_rejected)
  without executing; the failure handler decides on recovery as for any failure.
Without a policy handler, action tasks stay READY. When a pass ends, a completion
handler (`RunCompletion`) appends RunCompleted if nothing blocks it.

Duplicate-execution protection
------------------------------
- Process-local: a scheduler never launches a task that is already in its in-flight set.
- Database-backed: a task only executes after its `TaskStarted` event is committed. The
  projector accepts `TaskStarted` only for a READY task, and the append uses
  `expected_sequence` (Phase 1 optimistic concurrency on the run row). If two schedulers,
  in one process or several sharing the database, claim the same task, exactly one
  append commits. The other gets `SequenceConflictError`, re-reads, sees the task
  RUNNING and skips it.

This is not a distributed scheduler: there are no leases, heartbeats or crash recovery.
A task whose scheduler dies after claiming it stays RUNNING.
"""

import asyncio
import logging
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.core.exceptions import (
    InvalidEventError,
    RunNotActiveError,
    SchedulerContentionError,
    SequenceConflictError,
)
from app.core.redaction import safe_message
from app.events.base import NewEvent
from app.events.types import (
    EventPayload,
    FailureType,
    RunFailed,
    TaskCompleted,
    TaskFailed,
    TaskStarted,
)
from app.models.run import TERMINAL_RUN_STATUSES, RunStatus
from app.orchestration.task_executor import TaskExecutionResult, TaskExecutor
from app.orchestration.task_graph import TaskGraph
from app.persistence.database import Database
from app.recovery.classifier import classify_result
from app.conflicts.manager import ConflictOutcome
from app.verification.manager import VerificationOutcome
from app.policy.manager import GateOutcome
from app.policy.rules import EXECUTABLE, REFUSED
from app.recovery.schemas import RecoveryAction, RecoveryOutcome
from app.services.runs import RunService
from app.state.context_builder import TaskContext, build_task_context
from app.state.models import RunState, TaskState, TaskStatus
from app.state.projector import apply_all

logger = logging.getLogger(__name__)

# Sequence conflicts only happen when another writer appended in between, so each retry
# means someone else made progress. The bound turns pathological contention into an error.
MAX_APPEND_ATTEMPTS = 50


class ScheduleReport(BaseModel):
    """What one `Scheduler.run` call did, plus the task statuses when it finished."""

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    started: list[str]
    completed: list[str]
    failed: list[str]
    task_statuses: dict[str, TaskStatus]
    # Phase 5: tasks created by recovery during this call, and the run status at the end.
    replanned: list[str] = []
    run_status: RunStatus = RunStatus.CREATED
    # Phase 6: conflicts detected / resolved / concluded unresolved during this call.
    conflicts_detected: list[str] = []
    conflicts_resolved: list[str] = []
    conflicts_unresolved: list[str] = []
    # Phase 7: checkpoints that passed / failed their verification during this call, and
    # READY checkpoints not started (deferred, or no verification handler).
    verifications_passed: list[str] = []
    verifications_failed: list[str] = []
    verifications_waiting: list[str] = []
    # Phase 8: approvals still pending when the call ended, actions refused at the gate
    # during this call, and why the run is not completed (empty once completed).
    approvals_pending: list[str] = []
    actions_refused: list[str] = []
    completion_blockers: list[str] = []


class FailureHandler(Protocol):
    """Called after a TaskFailed is recorded (implemented by RecoveryManager)."""

    async def handle_failure(self, run_id: UUID, task_id: str) -> RecoveryOutcome: ...


class ConflictHandler(Protocol):
    """Called after a TaskCompleted is recorded (implemented by ConflictManager)."""

    async def after_task_completed(self, run_id: UUID, task_id: str | None) -> ConflictOutcome: ...


class VerificationHandler(Protocol):
    """Runs a claimed verification checkpoint (implemented by VerificationManager)."""

    def deferral(self, state: RunState, task_id: str) -> str | None: ...

    async def run(self, run_id: UUID, task_id: str) -> VerificationOutcome: ...


class PolicyHandler(Protocol):
    """Gates and runs action tasks (implemented by PolicyManager)."""

    async def gate(self, run_id: UUID, task_id: str) -> GateOutcome: ...

    async def execute(self, run_id: UUID, task_id: str, state: RunState) -> TaskExecutionResult: ...


class CompletionHandler(Protocol):
    """Completes the run when nothing blocks it (implemented by RunCompletion)."""

    async def complete_if_ready(self, run_id: UUID) -> list[str]: ...


class Scheduler:
    def __init__(
        self,
        database: Database,
        executor: TaskExecutor,
        recovery: FailureHandler | None = None,
        conflicts: ConflictHandler | None = None,
        verification: VerificationHandler | None = None,
        policy: PolicyHandler | None = None,
        completion: CompletionHandler | None = None,
    ) -> None:
        self._database = database
        self._executor = executor
        self._recovery = recovery
        self._conflicts = conflicts
        self._verification = verification
        self._policy = policy
        self._completion = completion

    async def run(self, run_id: UUID) -> ScheduleReport:
        state = await self._load_state(run_id)
        if state.status in TERMINAL_RUN_STATUSES:
            raise RunNotActiveError(f"run {run_id} is {state.status.value}")

        in_flight: dict[asyncio.Task[TaskExecutionResult | VerificationOutcome], str] = {}
        started: list[str] = []
        completed: list[str] = []
        failed: list[str] = []
        replanned: list[str] = []
        verdicts: dict[str, list[str]] = {"passed": [], "failed": []}
        waiting: set[str] = set()
        refused: list[str] = []
        blockers: list[str] = []
        conflict_outcomes: list[ConflictOutcome] = []
        run_failure: str | None = None  # set by recovery: stop starting tasks, then fail
        if self._conflicts is not None:
            conflict_outcomes.append(await self._conflicts.after_task_completed(run_id, None))
            state = await self._load_state(run_id)
        try:
            while True:
                if state.status not in TERMINAL_RUN_STATUSES and run_failure is None:
                    running_here = set(in_flight.values())
                    for task_id in TaskGraph.from_tasks(state.tasks.values()).runnable():
                        if task_id in running_here:
                            continue
                        checkpoint = state.tasks[task_id].verification is not None
                        if checkpoint and not self._may_verify(state, task_id):
                            waiting.add(task_id)
                            continue
                        action = state.tasks[task_id].action is not None
                        if action:
                            if self._policy is None:
                                logger.info("action %r is ready, but no policy handler is configured", task_id)
                                continue
                            gate = await self._policy.gate(run_id, task_id)
                            if gate.status not in EXECUTABLE | REFUSED:
                                continue  # awaiting approval: nothing executes
                            if gate.status in REFUSED:
                                refused.append(task_id)
                        claimed = await self._claim(run_id, task_id)
                        if claimed is None:
                            continue  # another scheduler got there first
                        waiting.discard(task_id)
                        started.append(task_id)
                        execution: asyncio.Task[TaskExecutionResult | VerificationOutcome]
                        if checkpoint:
                            assert self._verification is not None
                            execution = asyncio.create_task(self._verification.run(run_id, task_id))
                        elif action:
                            assert self._policy is not None
                            execution = asyncio.create_task(self._policy.execute(run_id, task_id, claimed))
                        else:
                            execution = asyncio.create_task(
                                self._execute(claimed.tasks[task_id], build_task_context(claimed, task_id))
                            )
                        in_flight[execution] = task_id

                if not in_flight:
                    break

                done, _ = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
                for execution in sorted(done, key=lambda item: in_flight[item]):
                    task_id = in_flight.pop(execution)
                    result = execution.result()
                    if isinstance(result, VerificationOutcome):
                        # The verification handler recorded the verdict and outcome itself.
                        if not result.recorded:
                            continue
                        task_failed = not result.passed
                        if result.verdict is not None:
                            verdicts[result.verdict].append(task_id)
                    else:
                        task_failed = await self._record(run_id, task_id, result)
                    (failed if task_failed else completed).append(task_id)
                    if not task_failed and self._conflicts is not None:
                        conflict_outcomes.append(await self._conflicts.after_task_completed(run_id, task_id))
                    if task_failed and self._recovery is not None and run_failure is None:
                        outcome = await self._recovery.handle_failure(run_id, task_id)
                        if outcome.action is RecoveryAction.REPLAN:
                            replanned.extend(outcome.new_task_ids)
                        elif outcome.action is RecoveryAction.FAIL_RUN:
                            run_failure = outcome.reason
                state = await self._load_state(run_id)

            if run_failure is not None:
                await self._fail_run(run_id, run_failure)
            elif self._completion is not None:
                blockers = await self._completion.complete_if_ready(run_id)
        except BaseException:
            for execution in in_flight:
                execution.cancel()
            await asyncio.gather(*in_flight, return_exceptions=True)
            raise

        final = await self._load_state(run_id)
        return ScheduleReport(
            run_id=run_id,
            started=started,
            completed=completed,
            failed=failed,
            task_statuses={task_id: task.status for task_id, task in sorted(final.tasks.items())},
            replanned=replanned,
            run_status=final.status,
            conflicts_detected=[c for o in conflict_outcomes for c in o.detected],
            conflicts_resolved=[c for o in conflict_outcomes for c in o.resolved],
            conflicts_unresolved=[c for o in conflict_outcomes for c in o.unresolved],
            verifications_passed=verdicts["passed"],
            verifications_failed=verdicts["failed"],
            verifications_waiting=sorted(t for t in waiting if final.tasks[t].status is TaskStatus.READY),
            approvals_pending=sorted(a.approval_id for a in final.approvals.values() if a.status.value == "pending"),
            actions_refused=refused,
            completion_blockers=blockers,
        )

    def _may_verify(self, state: RunState, task_id: str) -> bool:
        """A READY checkpoint starts only with a verification handler, and only when no
        relevant conflict resolution is pending."""
        if self._verification is None:
            logger.info("checkpoint %r is ready, but no verification handler is configured", task_id)
            return False
        reason = self._verification.deferral(state, task_id)
        if reason is not None:
            logger.info("checkpoint %r deferred: %s", task_id, reason)
            return False
        return True

    async def _execute(self, task: TaskState, context: TaskContext) -> TaskExecutionResult:
        try:
            return await self._executor.execute(task, context)
        except Exception as exc:
            # Recorded as a TaskFailed event, so the failure is visible in the log.
            logger.exception("executor raised while running task %r", task.task_id)
            return TaskExecutionResult.failure(
                f"executor raised {type(exc).__name__}: {exc}", error_type="executor_error"
            )

    async def _claim(self, run_id: UUID, task_id: str) -> RunState | None:
        """Append TaskStarted if the task is still READY.

        Returns the state including the committed TaskStarted, or None if the task is no
        longer claimable."""
        for _ in range(MAX_APPEND_ATTEMPTS):
            async with self._database.session_factory() as session:
                service = RunService(session)
                state = await service.get_state(run_id)
                task = state.tasks.get(task_id)
                if task is None or task.status is not TaskStatus.READY:
                    return None
                if state.status in TERMINAL_RUN_STATUSES:
                    return None
                try:
                    stored = await service.append_events(
                        run_id,
                        [NewEvent(payload=TaskStarted(task_id=task_id), task_id=task_id)],
                        expected_sequence=state.last_sequence,
                    )
                except SequenceConflictError:
                    continue
                claimed = apply_all(state, stored)
                assert claimed is not None
                return claimed
        raise SchedulerContentionError(
            f"could not claim task {task_id!r} after {MAX_APPEND_ATTEMPTS} attempts"
        )

    async def _record(self, run_id: UUID, task_id: str, result: TaskExecutionResult) -> bool:
        """Record the outcome: the executor's events (tool events always; facts/artifacts
        only on success) plus TaskCompleted or TaskFailed, in one atomic append.
        Returns True if the task was recorded as failed.

        Events of a type the executor may not emit, or that the projector rejects, are
        not recorded; the task is then recorded as failed with the reason."""
        agent_id = result.agent_id
        disallowed = sorted(
            {e.event_type.value for e in result.events}
            - {t.value for t in result.allowed_event_types()}
        )
        if disallowed:
            error = f"executor returned disallowed event types {disallowed}; nothing recorded"
            failed = _task_failed(task_id, error, FailureType.POLICY_FAILURE, "disallowed_events")
            await self._append(run_id, [failed], task_id, agent_id)
            return True

        outcome: EventPayload
        if result.succeeded:
            outcome = TaskCompleted(
                task_id=task_id,
                summary=result.summary,
                evidence=result.evidence,
                metadata=result.metadata,
            )
        else:
            classification = classify_result(result)
            outcome = TaskFailed(
                task_id=task_id,
                error=classification.message,
                failure_type=classification.failure_type,
                error_type=classification.error_type,
                tool_call_id=classification.tool_call_id,
            )
        try:
            await self._append(run_id, [*result.events, outcome], task_id, agent_id)
        except InvalidEventError as exc:
            if not result.events:
                raise
            error = f"result events rejected: {exc.message}"
            if not result.succeeded:
                error = f"{result.error}; {error}"
            failed = _task_failed(task_id, error, FailureType.VALIDATION_FAILURE, "result_rejected")
            await self._append(run_id, [failed], task_id, agent_id)
            return True
        return not result.succeeded

    async def _fail_run(self, run_id: UUID, reason: str) -> None:
        drafts = [NewEvent(payload=RunFailed(reason=safe_message(reason)))]
        for _ in range(MAX_APPEND_ATTEMPTS):
            async with self._database.session_factory() as session:
                service = RunService(session)
                if (await service.get_state(run_id)).status in TERMINAL_RUN_STATUSES:
                    return
                try:
                    await service.append_events(run_id, drafts)
                except SequenceConflictError:
                    continue
                return
        raise SchedulerContentionError(f"could not record RunFailed after {MAX_APPEND_ATTEMPTS} attempts")

    async def _append(
        self, run_id: UUID, payloads: list[EventPayload], task_id: str, agent_id: str | None
    ) -> None:
        # The envelope records which task (and agent, if any) the events were produced for.
        drafts = [NewEvent(payload=p, task_id=task_id, agent_id=agent_id) for p in payloads]
        for _ in range(MAX_APPEND_ATTEMPTS):
            async with self._database.session_factory() as session:
                try:
                    await RunService(session).append_events(run_id, drafts)
                except SequenceConflictError:
                    continue
                return
        kinds = ", ".join(p.event_type.value for p in payloads)
        raise SchedulerContentionError(
            f"could not record {kinds} after {MAX_APPEND_ATTEMPTS} attempts"
        )

    async def _load_state(self, run_id: UUID) -> RunState:
        async with self._database.session_factory() as session:
            return await RunService(session).get_state(run_id)


def _task_failed(task_id: str, error: str, failure_type: FailureType, error_type: str) -> TaskFailed:
    return TaskFailed(
        task_id=task_id, error=safe_message(error), failure_type=failure_type, error_type=error_type
    )
