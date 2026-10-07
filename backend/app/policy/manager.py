"""PolicyManager and ApprovalManager: the only writers of policy and approval events.

    Scheduler sees a READY action task
        -> PolicyManager.gate(run_id, task_id)
             no decision yet: PolicyEngine.evaluate (deterministic, from tool metadata and
             the run's history) -> append PolicyEvaluated (+ ApprovalRequested if
             APPROVAL_REQUIRED), atomically, with optimistic concurrency
        <- the action's status: allowed / approved (claim and execute),
           denied / rejected (claim and fail), awaiting_approval (leave it READY)
    Scheduler claims the task (TaskStarted)
        -> PolicyManager.execute: an Authorization from recorded state -> ActionTaskExecutor,
           or, for a refused action, a failure (action_denied / approval_rejected)
        <- TaskExecutionResult, recorded by the scheduler

    Human (POST .../approvals/{id}/approve | reject)
        -> ApprovalManager.decide: the approval exists in this run, is pending, its task has
           not started -> append ApprovalGranted / ApprovalRejected. Nothing is executed
           here; the next scheduler pass does that through the normal path.

Neither manager calls an LLM. Agents and models have no route to either.
"""

import logging
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.core.exceptions import (
    ApprovalNotFoundError,
    ApprovalNotPendingError,
    RunNotActiveError,
    SchedulerContentionError,
    SequenceConflictError,
)
from app.core.redaction import safe_message
from app.events.base import NewEvent
from app.events.types import ApprovalGranted, ApprovalRejected, ApprovalRequested, EventPayload, PolicyEvaluated
from app.models.run import TERMINAL_RUN_STATUSES
from app.orchestration.task_executor import TaskExecutionResult
from app.persistence.database import Database
from app.policy.actions import ActionTaskExecutor
from app.policy.engine import ActionRequest, PolicyEngine
from app.policy.rules import (
    REFUSAL_ERROR_TYPES,
    ActionStatus,
    action_status,
    approval_for_task,
    approval_id_for,
    authorization_for,
    rejected_fingerprints,
)
from app.services.runs import RunService
from app.state.models import ApprovalState, ApprovalStatus, NOT_STARTED_STATUSES, RunState, TaskStatus
from app.tools.errors import UnknownToolError

logger = logging.getLogger(__name__)

POLICY_AGENT_ID = "policy_gate"
APPROVAL_AGENT_ID = "human_approval"
MAX_APPEND_ATTEMPTS = 50


class GateOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    status: ActionStatus
    approval_id: str | None = None


class PolicyManager:
    def __init__(self, database: Database, engine: PolicyEngine, actions: ActionTaskExecutor) -> None:
        self._database = database
        self._engine = engine
        self._actions = actions

    async def gate(self, run_id: UUID, task_id: str) -> GateOutcome:
        for _ in range(MAX_APPEND_ATTEMPTS):
            async with self._database.session_factory() as session:
                service = RunService(session)
                state = await service.get_state(run_id)
                task = state.tasks[task_id]
                assert task.action is not None
                if task_id in state.policy or task.status is not TaskStatus.READY:
                    return self._outcome(state, task_id)
                try:
                    definition = self._actions.tool_executor.registry.get(task.action.tool_name).definition
                except UnknownToolError:
                    definition = None
                decision = self._engine.evaluate(
                    ActionRequest(task_id=task_id, tool_name=task.action.tool_name, arguments=dict(task.action.arguments)),
                    definition,
                    rejected_fingerprints=rejected_fingerprints(state),
                )
                payloads: list[EventPayload] = [PolicyEvaluated(decision=decision)]
                if decision.approval_required:
                    payloads.append(
                        ApprovalRequested(
                            approval_id=approval_id_for(task_id),
                            task_id=task_id,
                            action=task.action,
                            description=safe_message(
                                f"{task.title}: {task.action.intent} [tool {task.action.tool_name}, "
                                f"{decision.category.value if decision.category else 'unknown'}; {decision.reason}]",
                                max_chars=2000,
                            ),
                        )
                    )
                try:
                    await service.append_events(
                        run_id, [NewEvent(payload=p, agent_id=POLICY_AGENT_ID, task_id=task_id) for p in payloads],
                        expected_sequence=state.last_sequence,
                    )
                except SequenceConflictError:
                    continue
                logger.info("policy for action %r: %s (%s)", task_id, decision.outcome.value, decision.rule)
                return self._outcome(await service.get_state(run_id), task_id)
        raise SchedulerContentionError(f"could not record the policy decision after {MAX_APPEND_ATTEMPTS} attempts")

    @staticmethod
    def _outcome(state: RunState, task_id: str) -> GateOutcome:
        approval = approval_for_task(state, task_id)
        return GateOutcome(task_id=task_id, status=action_status(state, task_id),
                           approval_id=approval.approval_id if approval else None)

    async def execute(self, run_id: UUID, task_id: str, state: RunState) -> TaskExecutionResult:
        """Run a claimed action task: its exact action if authorized, else a refusal."""
        status = action_status(state, task_id)
        record = state.policy.get(task_id)
        if status in REFUSAL_ERROR_TYPES:
            if status == "denied":
                reason = record.decision.reason if record else "denied"
            else:
                approval = approval_for_task(state, task_id)
                reason = (approval.decision_reason if approval else None) or "rejected by the approver"
            return TaskExecutionResult(
                succeeded=False, error=f"action {status} by the policy gate: {reason}",
                error_type=REFUSAL_ERROR_TYPES[status], agent_id=POLICY_AGENT_ID,
            )
        authorization = authorization_for(state, task_id)
        if authorization is None:
            return TaskExecutionResult(
                succeeded=False, error=f"action {task_id!r} is not authorized ({status})",
                error_type="action_denied" if status == "denied" else "tool_failed", agent_id=POLICY_AGENT_ID,
            )
        return await self._actions.execute(run_id, state.tasks[task_id], authorization)


class ApprovalManager:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def decide(
        self, run_id: UUID, approval_id: str, *, granted: bool, actor: str, reason: str | None = None
    ) -> ApprovalState:
        """Record a human decision. Validates; never executes the action."""
        for _ in range(MAX_APPEND_ATTEMPTS):
            async with self._database.session_factory() as session:
                service = RunService(session)
                state = await service.get_state(run_id)
                if state.status in TERMINAL_RUN_STATUSES:
                    raise RunNotActiveError(f"run {run_id} is {state.status.value}")
                approval = state.approvals.get(approval_id)
                if approval is None:
                    raise ApprovalNotFoundError(f"approval {approval_id!r} not found in run {run_id}")
                if approval.status is not ApprovalStatus.PENDING:
                    raise ApprovalNotPendingError(f"approval {approval_id!r} is already {approval.status.value}")
                if state.tasks[approval.task_id].status not in NOT_STARTED_STATUSES:
                    raise ApprovalNotPendingError(
                        f"approval {approval_id!r}: task {approval.task_id!r} is {state.tasks[approval.task_id].status.value}"
                    )
                payload: EventPayload = (
                    ApprovalGranted(approval_id=approval_id, task_id=approval.task_id, actor=actor, reason=reason)
                    if granted
                    else ApprovalRejected(approval_id=approval_id, task_id=approval.task_id, actor=actor, reason=reason)
                )
                try:
                    await service.append_events(
                        run_id, [NewEvent(payload=payload, agent_id=APPROVAL_AGENT_ID, task_id=approval.task_id)],
                        expected_sequence=state.last_sequence,
                    )
                except SequenceConflictError:
                    continue
                logger.info("approval %r %s by %s", approval_id, "granted" if granted else "rejected", actor)
                return (await service.get_state(run_id)).approvals[approval_id]
        raise SchedulerContentionError(f"could not record the approval decision after {MAX_APPEND_ATTEMPTS} attempts")

