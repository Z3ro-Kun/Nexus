"""Pure rules over projected state for the policy gate and run completion. No I/O.

Used by the projector (to validate events), the PolicyManager (to decide what the
scheduler may do) and the API.

Action status (an action task's position at the gate):

    unevaluated        no PolicyEvaluated yet
    allowed            decision ALLOW                      -> may execute
    awaiting_approval  APPROVAL_REQUIRED, approval pending -> waits; never executes
    approved           APPROVAL_REQUIRED, approval granted -> may execute
    rejected           APPROVAL_REQUIRED, approval rejected -> fails, never executes
    denied             decision DENY                       -> fails, never executes

APPROVED is not EXECUTED: execution is the action task reaching COMPLETED with its
recorded tool call. EXECUTED is not VERIFIED: that is a passing verification checkpoint.
"""

from typing import Literal

from app.events.types import PolicyOutcome, action_fingerprint
from app.orchestration.task_graph import TaskGraph
from app.policy.engine import Authorization
from app.state.models import ApprovalState, ApprovalStatus, RunState, TaskStatus, WorkspaceArtifact
from app.verification.checkpoint import latest_attempts, run_verified

ActionStatus = Literal["unevaluated", "allowed", "awaiting_approval", "approved", "rejected", "denied"]
EXECUTABLE: frozenset[str] = frozenset({"allowed", "approved"})
REFUSED: frozenset[str] = frozenset({"rejected", "denied"})
# TaskFailed error types for actions that were refused at the gate.
REFUSAL_ERROR_TYPES = {"denied": "action_denied", "rejected": "approval_rejected"}


def approval_for_task(state: RunState, task_id: str) -> ApprovalState | None:
    return next((a for a in state.approvals.values() if a.task_id == task_id), None)


def approval_id_for(task_id: str) -> str:
    return f"{task_id}.approval"[:128]


def action_status(state: RunState, task_id: str) -> ActionStatus:
    record = state.policy.get(task_id)
    if record is None:
        return "unevaluated"
    outcome = record.decision.outcome
    if outcome is PolicyOutcome.ALLOW:
        return "allowed"
    if outcome is PolicyOutcome.DENY:
        return "denied"
    approval = approval_for_task(state, task_id)
    if approval is None or approval.status is ApprovalStatus.PENDING:
        return "awaiting_approval"
    return "approved" if approval.status is ApprovalStatus.GRANTED else "rejected"


def authorization_for(state: RunState, task_id: str) -> Authorization | None:
    """Permission to execute the task's exact action, from recorded state only."""
    task = state.tasks.get(task_id)
    if task is None or task.action is None or action_status(state, task_id) not in EXECUTABLE:
        return None
    decision = state.policy[task_id].decision
    if decision.action_fingerprint != action_fingerprint(task.action.tool_name, task.action.arguments):
        return None
    approval = approval_for_task(state, task_id)
    return Authorization(
        task_id=task_id,
        action_fingerprint=decision.action_fingerprint,
        decision=decision,
        approval_id=approval.approval_id if approval is not None and approval.status is ApprovalStatus.GRANTED else None,
    )


def rejected_fingerprints(state: RunState) -> frozenset[str]:
    """Actions a human rejected in this run (identical re-requests are denied)."""
    return frozenset(
        action_fingerprint(a.action.tool_name, a.action.arguments)
        for a in state.approvals.values()
        if a.status is ApprovalStatus.REJECTED
    )


def pending_deliveries(state: RunState) -> list[WorkspaceArtifact]:
    """Validated workspace deliverables (files and archives) not yet marked ready."""
    return [
        a for a in sorted(state.workspace_artifacts.values(), key=lambda a: a.sequence)
        if a.artifact_type in ("file", "archive") and a.status == "validated"
    ]


def artifact_blockers(state: RunState, *, ignore_pending_delivery: bool = False) -> list[str]:
    """Phase 10: workspace artifacts that keep the run from completing. A rejected
    artifact, an unvalidated one, a project without its archive, or (unless ignored) a
    deliverable not yet marked ready."""
    blockers: list[str] = []
    for a in sorted(state.workspace_artifacts.values(), key=lambda a: a.sequence):
        label = f"artifact {a.artifact_id} ({a.name})"
        if a.status == "rejected":
            blockers.append(f"{label} failed validation: {a.problems[0] if a.problems else 'no reason recorded'}")
        elif a.status == "created":
            blockers.append(f"{label} was not validated")
        elif a.artifact_type == "project" and a.archive_id is None:
            blockers.append(f"{label} was not packaged")
        elif a.status == "validated" and a.artifact_type != "project" and not ignore_pending_delivery:
            blockers.append(f"{label} is not ready for delivery")
    return blockers


def completion_blockers(state: RunState, *, ignore_pending_delivery: bool = False) -> list[str]:
    """Why the run cannot be completed; empty if it can. RunCompleted requires:
    every task resolved to COMPLETED (a failed task counts only through a completed
    replacement), no approval pending, no refused action left unreplaced, a passing
    current attempt of every verification checkpoint (at least one), and every workspace
    deliverable validated, packaged (projects) and ready (Phase 10). RunCompletion checks
    with `ignore_pending_delivery` and then records the readiness itself."""
    blockers: list[str] = []
    if not state.verifications:
        blockers.append("no verification checkpoint: the run has not been verified")
    elif not run_verified(state):
        blockers.extend(
            f"verification {v.verification_id} is {v.status.value}"
            for v in latest_attempts(state) if v.status.value != "passed"
        )
    for approval in sorted(state.approvals.values(), key=lambda a: a.approval_id):
        if approval.status is ApprovalStatus.PENDING:
            blockers.append(f"approval {approval.approval_id} for task {approval.task_id} is pending")
    resolved = TaskGraph.from_tasks(state.tasks.values()).resolved_statuses()
    for task_id, status in resolved.items():
        if status is TaskStatus.COMPLETED:
            continue
        task = state.tasks[task_id]
        detail = f"task {task_id} is {status.value}"
        if task.action is not None and action_status(state, task_id) in REFUSED:
            detail = f"action {task_id} was {action_status(state, task_id)} and not replaced"
        blockers.append(detail)
    blockers.extend(artifact_blockers(state, ignore_pending_delivery=ignore_pending_delivery))
    return blockers
