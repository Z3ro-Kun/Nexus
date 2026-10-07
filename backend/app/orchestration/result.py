"""Read models over projected state for the orchestration API: the run's phase and its
final result. Pure: derived from RunState only, never stored, never a second source of
truth.

Run phase (derived, not a new state machine):

    created               no tasks yet (planning has not happened)
    executing             work is running or ready to run
    waiting_for_approval  nothing can run until a human decides a pending approval
    verifying             a verification checkpoint is running or ready
    blocked               nothing can run and the run cannot complete (e.g. an unresolved
                          conflict, a failure that was not recovered)
    completed / failed    RunCompleted / RunFailed

Recovery and conflict resolution are not phases of their own: they add tasks, so the
run is `executing` while they run.
"""

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue

from app.events.types import FactClaim
from app.models.run import RunStatus
from app.orchestration.task_graph import TaskGraph
from app.policy.rules import action_status, completion_blockers
from app.state.conflict_detector import resolution_origin
from app.state.models import ApprovalStatus, RunState, TaskStatus, VerificationStatus
from app.verification.checkpoint import latest_attempts, run_verified

MAX_FACTS = 100
MAX_ARTIFACT_CHARS = 20_000


class RunPhase(str, Enum):
    CREATED = "created"
    EXECUTING = "executing"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    VERIFYING = "verifying"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_CLARIFICATION = "needs_clarification"  # Phase 11: not executable as stated


def run_phase(state: RunState) -> RunPhase:
    if state.status is RunStatus.COMPLETED:
        return RunPhase.COMPLETED
    if state.status is RunStatus.FAILED:
        return RunPhase.FAILED
    if state.status is RunStatus.NEEDS_CLARIFICATION:
        return RunPhase.NEEDS_CLARIFICATION
    if not state.tasks:
        return RunPhase.CREATED
    # A claimed checkpoint is verifying even before its VerificationStarted is recorded.
    if any(state.tasks[v].status is TaskStatus.RUNNING for v in state.verifications):
        return RunPhase.VERIFYING

    def waiting_on_approval(task_id: str) -> bool:
        return state.tasks[task_id].action is not None and action_status(state, task_id) == "awaiting_approval"

    active = [
        t for t in state.tasks.values()
        if t.verification is None and t.status in (TaskStatus.READY, TaskStatus.RUNNING) and not waiting_on_approval(t.task_id)
    ]
    if active:
        return RunPhase.EXECUTING
    if any(a.status is ApprovalStatus.PENDING for a in state.approvals.values()):
        return RunPhase.WAITING_FOR_APPROVAL
    if any(state.tasks[v.verification_id].status is TaskStatus.READY for v in latest_attempts(state)):
        return RunPhase.VERIFYING
    return RunPhase.BLOCKED


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ResultArtifact(_Frozen):
    artifact_id: str
    name: str
    media_type: str
    content: str
    truncated: bool


class ClarificationResult(_Frozen):
    """Phase 11: the objective could not be executed as stated; what to ask the user."""

    reason: str
    question: str
    missing: list[str]


class Deliverable(_Frozen):
    """A final piece of work: an agent task that no other agent task builds on. Action
    tasks are effects, not deliverables (see `approvals` / `tasks`), so a task an action
    depends on (e.g. the recommendation an order is based on) is still a deliverable."""

    task_id: str
    title: str
    agent_type: str | None
    summary: str | None
    artifacts: list[ResultArtifact]


class SupportingFact(_Frozen):
    fact_id: str
    task_id: str | None
    content: str
    claim: FactClaim | None
    provenance_kind: str | None
    source: str | None
    tool_name: str | None
    tool_call_id: str | None
    fake: bool


class TaskSummary(_Frozen):
    task_id: str
    title: str
    agent_type: str | None
    task_type: str | None
    status: TaskStatus
    replaces: str | None
    replaced_by: str | None
    conflict_id: str | None
    is_action: bool
    is_checkpoint: bool


class VerificationSummary(_Frozen):
    verification_id: str
    checkpoint_id: str
    attempt: int
    status: VerificationStatus
    failed_checks: list[str]
    semantic: str | None
    reason: str | None


class ApprovalSummary(_Frozen):
    approval_id: str
    task_id: str
    tool_name: str
    status: ApprovalStatus
    actor: str | None
    executed: bool


class ConflictSummary(_Frozen):
    conflict_id: str
    status: str
    fact_key: str | None
    fact_ids: list[str]
    accepted_fact_id: str | None
    reason: str | None


class RecoverySummary(_Frozen):
    replan_number: int
    outcome: str
    failed_task_id: str
    failure_type: str | None
    summary: str
    new_task_ids: list[str]


class FinalResult(_Frozen):
    """What a run produced and how, derived from final state (not the event log)."""

    run_id: UUID
    objective: str
    constraints: list[str]
    phase: RunPhase
    verified: bool
    completion_summary: str | None
    failure_reason: str | None
    completion_blockers: list[str]
    # Phase 11: set when the run needs clarification (phase needs_clarification).
    clarification: ClarificationResult | None = None
    deliverables: list[Deliverable]
    supporting_facts: list[SupportingFact]
    tasks: list[TaskSummary]
    verifications: list[VerificationSummary]
    approvals: list[ApprovalSummary]
    conflicts: list[ConflictSummary]
    recovery: list[RecoverySummary]
    tool_calls: dict[str, JsonValue]
    last_sequence: int


def _is_work(state: RunState, task_id: str) -> bool:
    task = state.tasks[task_id]
    return task.verification is None and resolution_origin(state, task_id) is None


def _deliverables(state: RunState) -> list[Deliverable]:
    """Effective agent tasks (a replaced task is represented by its replacement) that no
    other agent task depends on."""
    replaced = {t.task_id for t in state.tasks.values() if t.replaced_by is not None}
    depended = {
        d for t in state.tasks.values() if _is_work(state, t.task_id) and t.action is None for d in t.dependencies
    }
    graph = TaskGraph.from_tasks(state.tasks.values())
    order = graph.topological_order()
    out = []
    for task_id in order:
        task = state.tasks[task_id]
        if not _is_work(state, task_id) or task.action is not None or task_id in replaced:
            continue
        origin = task_id
        while (prev := state.tasks[origin].replaces) is not None:
            origin = prev
        if origin in depended or task_id in depended:
            continue
        artifacts = [a for a in sorted(state.artifacts.values(), key=lambda a: a.sequence) if a.task_id == task_id]
        out.append(Deliverable(
            task_id=task_id, title=task.title, agent_type=task.agent_type, summary=task.summary,
            artifacts=[ResultArtifact(artifact_id=a.artifact_id, name=a.name, media_type=a.media_type,
                                      content=a.content[:MAX_ARTIFACT_CHARS], truncated=len(a.content) > MAX_ARTIFACT_CHARS)
                       for a in artifacts],
        ))
    return out


def _supporting_facts(state: RunState) -> list[SupportingFact]:
    """Facts the verified result rests on: those the passing checkpoint covered, plus
    accepted conflict evidence; before verification, facts of completed tasks."""
    passed = [v for v in latest_attempts(state) if v.status is VerificationStatus.PASSED]
    if passed:
        ids = list(dict.fromkeys(f for v in passed for f in v.fact_ids))
    else:
        ids = [f.fact_id for f in sorted(state.facts.values(), key=lambda f: f.sequence)
               if f.task_id in state.tasks and state.tasks[f.task_id].status is TaskStatus.COMPLETED]
    ids += [c.resolved_fact_id for c in state.conflicts.values() if c.resolved_fact_id and c.resolved_fact_id not in ids]
    facts = []
    for fact_id in ids[:MAX_FACTS]:
        f = state.facts[fact_id]
        p = f.provenance
        facts.append(SupportingFact(
            fact_id=f.fact_id, task_id=f.task_id, content=f.content, claim=f.claim,
            provenance_kind=p.kind if p else None, source=p.source if p else f.source,
            tool_name=p.tool_name if p else None, tool_call_id=p.tool_call_id if p else None, fake=bool(p and p.fake),
        ))
    return facts


def build_final_result(state: RunState) -> FinalResult:
    phase = run_phase(state)
    calls = list(state.tool_calls.values())
    return FinalResult(
        run_id=state.run_id,
        objective=state.goal,
        constraints=list(state.constraints),
        phase=phase,
        verified=run_verified(state),
        completion_summary=state.completion_summary,
        failure_reason=state.failure_reason,
        completion_blockers=[] if phase in (RunPhase.COMPLETED, RunPhase.NEEDS_CLARIFICATION) else completion_blockers(state),
        clarification=(
            ClarificationResult(reason=state.clarification.reason, question=state.clarification.question,
                                missing=list(state.clarification.missing))
            if state.clarification is not None else None
        ),
        deliverables=_deliverables(state),
        supporting_facts=_supporting_facts(state),
        tasks=[
            TaskSummary(
                task_id=t.task_id, title=t.title, agent_type=t.agent_type, task_type=t.task_type, status=t.status,
                replaces=t.replaces, replaced_by=t.replaced_by, conflict_id=t.conflict_id,
                is_action=t.action is not None, is_checkpoint=t.verification is not None,
            )
            for t in sorted(state.tasks.values(), key=lambda t: t.created_at)
        ],
        verifications=[
            VerificationSummary(
                verification_id=v.verification_id, checkpoint_id=v.checkpoint_id, attempt=v.attempt, status=v.status,
                failed_checks=[c.check_id for c in v.checks if not c.passed],
                semantic=None if v.semantic is None else ("passed" if v.semantic.passed else "failed"),
                reason=v.reason,
            )
            for v in sorted(state.verifications.values(), key=lambda v: (v.checkpoint_id, v.attempt))
        ],
        approvals=[
            ApprovalSummary(
                approval_id=a.approval_id, task_id=a.task_id, tool_name=a.action.tool_name, status=a.status,
                actor=a.actor, executed=state.tasks[a.task_id].status is TaskStatus.COMPLETED,
            )
            for a in sorted(state.approvals.values(), key=lambda a: a.requested_sequence)
        ],
        conflicts=[
            ConflictSummary(
                conflict_id=c.conflict_id, status=c.status.value,
                fact_key=f"{c.fact_key.subject} / {c.fact_key.attribute}" if c.fact_key else None,
                fact_ids=list(c.fact_ids), accepted_fact_id=c.resolved_fact_id, reason=c.resolution or c.description,
            )
            for c in sorted(state.conflicts.values(), key=lambda c: c.detected_sequence or 0)
        ],
        recovery=[
            RecoverySummary(
                replan_number=r.replan_number, outcome=r.outcome, failed_task_id=r.failed_task_id,
                failure_type=r.failure_type.value if r.failure_type else None, summary=r.summary,
                new_task_ids=list(r.new_task_ids),
            )
            for r in state.recovery.history
        ],
        tool_calls={
            "total": len(calls),
            "succeeded": sum(1 for c in calls if c.status.value == "succeeded"),
            "failed": sum(1 for c in calls if c.status.value == "failed"),
            "by_tool": {name: sum(1 for c in calls if c.tool_name == name) for name in sorted({c.tool_name for c in calls})},
        },
        last_sequence=state.last_sequence,
    )
