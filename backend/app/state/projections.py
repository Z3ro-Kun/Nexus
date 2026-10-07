"""Per-event-type projection handlers: (current state, event) -> new state.

Handlers are pure. They never mutate their input; collections are copied before changes.
Rule violations raise `InvalidEventError`.

Task lifecycle (enforced here, so it holds for every writer, not just the scheduler):

    TaskCreated -> (PENDING | READY | BLOCKED, derived from dependencies)
    TaskStarted   only from READY           -> RUNNING
    TaskCompleted only from RUNNING         -> COMPLETED
    TaskFailed    only from RUNNING         -> FAILED
    TaskCancelled only if not yet started   -> CANCELLED

A task can therefore enter RUNNING at most once. After each task event the derived
statuses of all not-started tasks are recomputed from the dependency graph.

Tool events (Phase 4) maintain `RunState.tool_calls`: ToolCalled opens a call, and
ToolSucceeded / ToolFailed close it exactly once.

Recovery (Phase 5): TaskFailed records the failure classification on the task.
TaskCreated with `replaces` must name a FAILED task without a replacement (checked by the
task graph) and links the two tasks. ReplanTriggered / ReplanRejected must refer to a
FAILED task, carry the next replan number, and are summarized in `RunState.recovery`.
A ReplanTriggered without `failed_task_id` (possible before Phase 5) changes nothing.

Conflicts (Phase 6), structured form (see app.state.conflict_detector):
- ConflictDetected: >= 2 distinct existing facts, all with claims on `fact_key`, whose
  values really disagree with exactly `conflict_type`; the fingerprint must match and be
  new in the run. The facts themselves are never changed.
- TaskCreated with `conflict_id`: the conflict is OPEN and has no resolution task yet.
- ConflictResolved: the conflict is OPEN; the resolver task is COMPLETED and is the
  conflict's resolution task or a replacement of it; every evidence fact is usable
  evidence (its fact, tool-derived, same key, independent source); the evidence agrees;
  the accepted fact is one of the evidence facts; corroborated facts are conflicting
  facts with the accepted value.
- ConflictUnresolved: the conflict is OPEN and the resolver task is as above.
So a conflict cannot be resolved before it is detected, twice, without evidence, or by
a task that has not finished. Pre-Phase 6 free-text conflicts keep their old rules and
can only be resolved in the free-text form.

Verification (Phase 7; see app.verification.checks):
- TaskCreated with `verification` is a checkpoint: agent "verifier", task type
  "verification", at least one dependency, no conflict_id; its tool_evidence_tasks are
  covered by it. Only a checkpoint can replace a checkpoint, and only with the identical
  spec and a superset of its dependencies; a checkpoint never replaces a work task. A
  task with the verifier agent or verification type must carry a spec.
- VerificationStarted: the checkpoint task is RUNNING and was not started before; the
  recorded context (covered tasks, facts, artifacts, tool calls, conflicts,
  based_on_sequence) equals what the state gives.
- VerificationPassed / VerificationFailed: the verification is RUNNING; the recorded
  deterministic checks equal `run_checks` on the current state; Passed requires every
  check (and the semantic verdict, if the spec asks for one) to pass; Failed requires a
  failing check or semantic verdict.
- TaskCompleted of a checkpoint requires VerificationPassed; TaskFailed of a checkpoint
  is refused after VerificationPassed; failure type VERIFICATION_FAILURE is reserved for
  a checkpoint whose verification FAILED (and required for it).
So a worker cannot mark work as verified, and no writer can record a pass that the
deterministic checks contradict. Verification events without `task_id` (the pre-Phase 7
form) are recorded only.

Policy gate and approvals (Phase 8; see app.policy.rules):
- TaskCreated with `action` is an action task: agent "action_executor", task type
  "action", no verification spec or conflict_id (and the agent/type require an action).
- PolicyEvaluated: one decision per action task, while it is READY; the decision names
  the task's exact tool and action fingerprint; an action identical to one a human
  rejected in this run can only be denied.
- ApprovalRequested: the task's decision is APPROVAL_REQUIRED, the action matches, one
  approval per task. ApprovalGranted / ApprovalRejected: the approval exists, belongs to
  the named task and is still pending (so no second decision, no grant after a
  rejection and vice versa).
- An action task starts only once the gate decided it may run (allowed / approved) or
  that it must fail (denied / rejected); it completes only if allowed or approved, after
  a successful tool call that is exactly its action; tool calls in its context must be
  exactly its action and authorized; refused actions fail with error type
  action_denied / approval_rejected (and only they do).
- RunCompleted: only if `completion_blockers` is empty (every task resolved to
  COMPLETED, no pending approval, no unreplaced refused action, verification passed,
  every workspace deliverable ready).

Clarification (Phase 11):
- ClarificationRequested: only while the run has no tasks; the run becomes
  needs_clarification, a terminal status, so nothing (no task, no RunCompleted or
  RunFailed) can follow it.

Workspace artifacts (Phase 10):
- ArtifactCreated: a new id (also among text artifacts), in a task's context, citing
  successful artifact_write calls of that task and facts that exist.
- ArtifactFileAdded: the artifact is still being created and belongs to the envelope's
  task; the path is a safe, canonical relative path, unique case-insensitively; the tool
  call is one of the artifact's artifact_write calls.
- ArtifactValidated: passed only for a created artifact, with counts that match its files
  (a single-file artifact has exactly one); failed for a created one, or for a validated
  one whose stored bytes no longer check out before delivery.
- ArtifactPackaged: a validated project, once; the archive lists exactly its paths.
- ArtifactReady: a validated file or archive with the recorded checksum, in a run whose
  verification passed.
- Supersession: ArtifactCreated with `supersedes` is accepted only from a task created by
  an accepted replan, naming a still-current (validated) artifact of the same type and name
  made by one of the task's direct dependencies. The old artifact (and its archive) becomes
  superseded only when the new one passes validation; its files and checksums are kept.
Approval events without `task_id` (the pre-Phase 8 form) are recorded only, unless they
name a Phase 8 approval.
"""

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from app.core.exceptions import InvalidEventError, TaskGraphError
from app.events.base import Event
from app.artifacts.safety import UnsafeArtifactError, check_name, normalize_path
from app.events.types import (
    ArtifactAdded,
    ArtifactCreated,
    ArtifactFileAdded,
    ArtifactPackaged,
    ArtifactReady,
    ArtifactValidated,
    ClarificationRequested,
    ConflictDetected,
    ConflictResolved,
    ConflictUnresolved,
    EventPayload,
    EventType,
    FactAdded,
    ReplanRejected,
    ReplanTriggered,
    RunCompleted,
    RunCreated,
    RunFailed,
    TaskCancelled,
    TaskCompleted,
    TaskCreated,
    TaskFailed,
    TaskStarted,
    ToolCalled,
    ToolFailed,
    ToolSucceeded,
    VERIFICATION_TASK_TYPE,
    VERIFIER_AGENT_TYPE,
    FailureType,
    VerificationFailed,
    VerificationPassed,
    VerificationStarted,
    ACTION_AGENT_TYPE,
    ACTION_TASK_TYPE,
    ApprovalGranted,
    ApprovalRejected,
    ApprovalRequested,
    PolicyEvaluated,
    PolicyOutcome,
    action_fingerprint,
)
from app.models.run import RunStatus
from app.orchestration.task_graph import TaskGraph
from app.state.context_builder import is_recovery_task, resolve_replacement
from app.state.conflict_detector import (
    assess_evidence,
    canonical_value,
    classify_disagreement,
    conflict_fingerprint,
    fact_key,
    resolution_origin,
)
from app.state.models import (
    NOT_STARTED_STATUSES,
    Artifact,
    ArtifactFile,
    ClarificationState,
    WorkspaceArtifact,
    ConflictState,
    ConflictStatus,
    Fact,
    RecoveryRecord,
    RunState,
    TaskFailure,
    TaskState,
    TaskStatus,
    ToolCallState,
    ToolCallStatus,
    VerificationState,
    VerificationStatus,
    ApprovalState,
    ApprovalStatus,
    PolicyRecord,
)
from app.policy.rules import (
    EXECUTABLE,
    REFUSAL_ERROR_TYPES,
    REFUSED,
    action_status,
    approval_for_task,
    completion_blockers,
    rejected_fingerprints,
)
from app.verification.checks import context_refs, covered_tasks, effective_task, failed_references, run_checks
from app.verification.checkpoint import run_verified

Handler = Callable[[RunState, Event], RunState]
P = TypeVar("P", bound=EventPayload)


def _payload(event: Event, cls: type[P]) -> P:
    if not isinstance(event.payload, cls):
        raise InvalidEventError(
            f"event {event.sequence}: {event.event_type.value} carries "
            f"{type(event.payload).__name__}, expected {cls.__name__}"
        )
    return event.payload


def initial_state(event: Event) -> RunState:
    payload = _payload(event, RunCreated)
    return RunState(
        run_id=event.run_id,
        goal=payload.goal,
        constraints=tuple(payload.constraints),
        status=RunStatus.CREATED,
        last_sequence=event.sequence,
        updated_at=event.timestamp,
    )


def _with_derived_statuses(tasks: Mapping[str, TaskState]) -> dict[str, TaskState]:
    statuses = TaskGraph.from_tasks(tasks.values()).statuses()
    return {
        task_id: task
        if task.status is statuses[task_id]
        else task.model_copy(update={"status": statuses[task_id]})
        for task_id, task in tasks.items()
    }


_WAITING = frozenset({VerificationStatus.PENDING, VerificationStatus.BLOCKED})


def _sync_verifications(state: RunState) -> RunState:
    """Bring verification statuses that follow their task's status up to date: waiting
    (PENDING / BLOCKED), CANCELLED, and ERROR (the task failed without a verdict)."""
    if not state.verifications:
        return state
    changed: dict[str, VerificationState] = {}
    for vid, v in state.verifications.items():
        task = state.tasks[vid]
        status = v.status
        if v.status in _WAITING:
            status = {
                TaskStatus.BLOCKED: VerificationStatus.BLOCKED,
                TaskStatus.CANCELLED: VerificationStatus.CANCELLED,
                TaskStatus.FAILED: VerificationStatus.ERROR,
            }.get(task.status, VerificationStatus.PENDING)
        elif v.status is VerificationStatus.RUNNING and task.status is TaskStatus.FAILED:
            status = VerificationStatus.ERROR
        if status is not v.status or task.replaced_by != v.replaced_by:
            changed[vid] = v.model_copy(update={"status": status, "replaced_by": task.replaced_by})
    if not changed:
        return state
    return state.model_copy(update={"verifications": {**state.verifications, **changed}})


def _replace_task(state: RunState, task: TaskState, **changes: Any) -> RunState:
    tasks = {**state.tasks, task.task_id: task.model_copy(update=changes)}
    return _sync_verifications(state.model_copy(update={"tasks": _with_derived_statuses(tasks)}))


def _existing_task(state: RunState, task_id: str) -> TaskState:
    task = state.tasks.get(task_id)
    if task is None:
        raise InvalidEventError(f"task {task_id!r} does not exist")
    return task


def _transition(
    state: RunState,
    task_id: str,
    *,
    allowed_from: frozenset[TaskStatus],
    to: TaskStatus,
    event_name: str,
    **changes: Any,
) -> RunState:
    task = _existing_task(state, task_id)
    if task.status not in allowed_from:
        allowed = ", ".join(sorted(status.value for status in allowed_from))
        raise InvalidEventError(
            f"{event_name}: task {task_id!r} is {task.status.value}; requires {allowed}"
        )
    return _replace_task(state, task, status=to, **changes)


def task_created(state: RunState, event: Event) -> RunState:
    payload = _payload(event, TaskCreated)
    try:
        TaskGraph.from_tasks(state.tasks.values()).plan_additions([payload])
    except TaskGraphError as exc:
        raise InvalidEventError(f"TaskCreated {payload.task_id!r}: {exc.message}") from exc
    conflicts = state.conflicts
    if payload.conflict_id is not None:
        conflict = conflicts.get(payload.conflict_id)
        if conflict is None or conflict.fact_key is None:
            raise InvalidEventError(
                f"TaskCreated {payload.task_id!r}: unknown structured conflict {payload.conflict_id!r}"
            )
        if conflict.status is not ConflictStatus.OPEN or conflict.resolution_task_id is not None:
            raise InvalidEventError(
                f"TaskCreated {payload.task_id!r}: conflict {payload.conflict_id!r} is "
                f"{conflict.status.value} with resolution task {conflict.resolution_task_id!r}"
            )
        if payload.replaces is not None:
            raise InvalidEventError(f"TaskCreated {payload.task_id!r}: a resolution task cannot replace a task")
        conflicts = {**conflicts, conflict.conflict_id: conflict.model_copy(update={"resolution_task_id": payload.task_id})}
    _check_checkpoint(state, payload)
    _check_action_task(payload)
    task = TaskState(
        task_id=payload.task_id,
        title=payload.title,
        description=payload.description,
        task_type=payload.task_type,
        agent_type=payload.agent_type,
        parent_id=payload.parent_id,
        dependencies=tuple(payload.dependencies),
        replaces=payload.replaces,
        conflict_id=payload.conflict_id,
        verification=payload.verification,
        action=payload.action,
        created_at=event.timestamp,
    )
    tasks = {**state.tasks, task.task_id: task}
    if payload.replaces is not None:
        replaced = tasks[payload.replaces]
        tasks[payload.replaces] = replaced.model_copy(update={"replaced_by": task.task_id})
    verifications = state.verifications
    if payload.verification is not None:
        previous = state.verifications.get(payload.replaces or "")
        verifications = {
            **verifications,
            task.task_id: VerificationState(
                verification_id=task.task_id,
                checkpoint_id=previous.checkpoint_id if previous else task.task_id,
                attempt=previous.attempt + 1 if previous else 1,
                spec=payload.verification,
                dependencies=task.dependencies,
                created_at=event.timestamp,
            ),
        }
    new_state = state.model_copy(
        update={"tasks": _with_derived_statuses(tasks), "conflicts": conflicts, "verifications": verifications}
    )
    if payload.verification is not None:
        covered = set(covered_tasks(new_state, task.task_id))
        uncovered = [
            t for t in payload.verification.tool_evidence_tasks
            if t not in new_state.tasks or effective_task(new_state, t) not in covered
        ]
        if uncovered:
            raise InvalidEventError(
                f"TaskCreated {task.task_id!r}: tool_evidence_tasks {uncovered} are not covered by the checkpoint"
            )
    return _sync_verifications(new_state)


def _check_action_task(payload: TaskCreated) -> None:
    is_action = payload.action is not None or ACTION_AGENT_TYPE in (payload.agent_type, payload.task_type) \
        or payload.task_type == ACTION_TASK_TYPE
    if not is_action:
        return
    name = f"TaskCreated {payload.task_id!r}"
    if payload.action is None:
        raise InvalidEventError(f"{name}: an action_executor / action task needs an action spec")
    if (payload.agent_type, payload.task_type) != (ACTION_AGENT_TYPE, ACTION_TASK_TYPE):
        raise InvalidEventError(f"{name}: an action task has agent_type {ACTION_AGENT_TYPE!r} and task_type {ACTION_TASK_TYPE!r}")
    if payload.verification is not None or payload.conflict_id is not None:
        raise InvalidEventError(f"{name}: an action task cannot be a checkpoint or a conflict resolution task")


def _check_checkpoint(state: RunState, payload: TaskCreated) -> None:
    """Structural rules for verification checkpoints (see the module docstring)."""
    name = f"TaskCreated {payload.task_id!r}"
    is_checkpoint = (
        payload.verification is not None
        or payload.agent_type == VERIFIER_AGENT_TYPE
        or payload.task_type == VERIFICATION_TASK_TYPE
    )
    replaced = state.tasks.get(payload.replaces) if payload.replaces is not None else None
    if not is_checkpoint:
        if replaced is not None and replaced.verification is not None:
            raise InvalidEventError(f"{name}: only a verification checkpoint can replace checkpoint {replaced.task_id!r}")
        return
    if payload.verification is None:
        raise InvalidEventError(f"{name}: a verifier / verification task needs a verification spec")
    if (payload.agent_type, payload.task_type) != (VERIFIER_AGENT_TYPE, VERIFICATION_TASK_TYPE):
        raise InvalidEventError(
            f"{name}: a checkpoint has agent_type {VERIFIER_AGENT_TYPE!r} and task_type {VERIFICATION_TASK_TYPE!r}"
        )
    if payload.conflict_id is not None:
        raise InvalidEventError(f"{name}: a checkpoint cannot be a conflict resolution task")
    if not payload.dependencies:
        raise InvalidEventError(f"{name}: a checkpoint must depend on the work it verifies")
    if replaced is not None:
        if replaced.verification is None:
            raise InvalidEventError(f"{name}: a checkpoint cannot replace work task {replaced.task_id!r}")
        if payload.verification != replaced.verification:
            raise InvalidEventError(f"{name}: a replacement checkpoint must keep the requirements of {replaced.task_id!r}")
        if not set(replaced.dependencies) <= set(payload.dependencies):
            raise InvalidEventError(f"{name}: a replacement checkpoint must cover everything {replaced.task_id!r} covered")


def task_started(state: RunState, event: Event) -> RunState:
    payload = _payload(event, TaskStarted)
    task = state.tasks.get(payload.task_id)
    if task is not None and task.action is not None:
        status = action_status(state, payload.task_id)
        if status not in EXECUTABLE | REFUSED:
            raise InvalidEventError(f"TaskStarted: action {payload.task_id!r} is {status}; the policy gate has not let it run")
    return _transition(
        state,
        payload.task_id,
        allowed_from=frozenset({TaskStatus.READY}),
        to=TaskStatus.RUNNING,
        event_name="TaskStarted",
        started_at=event.timestamp,
    )


def task_completed(state: RunState, event: Event) -> RunState:
    payload = _payload(event, TaskCompleted)
    verification = state.verifications.get(payload.task_id)
    if verification is not None and verification.status is not VerificationStatus.PASSED:
        raise InvalidEventError(
            f"TaskCompleted: checkpoint {payload.task_id!r} can only complete after VerificationPassed "
            f"(verification is {verification.status.value})"
        )
    task = state.tasks.get(payload.task_id)
    if task is not None and task.action is not None:
        status = action_status(state, payload.task_id)
        if status not in EXECUTABLE:
            raise InvalidEventError(f"TaskCompleted: action {payload.task_id!r} is {status}; it may not complete")
        if not any(
            c.task_id == payload.task_id and c.status is ToolCallStatus.SUCCEEDED
            and c.tool_name == task.action.tool_name and c.arguments == task.action.arguments
            for c in state.tool_calls.values()
        ):
            raise InvalidEventError(f"TaskCompleted: action {payload.task_id!r} has no successful call of its action")
    return _transition(
        state,
        payload.task_id,
        allowed_from=frozenset({TaskStatus.RUNNING}),
        to=TaskStatus.COMPLETED,
        event_name="TaskCompleted",
        summary=payload.summary,
        evidence=tuple(payload.evidence),
        result_metadata=dict(payload.metadata),
        completed_at=event.timestamp,
    )


def task_failed(state: RunState, event: Event) -> RunState:
    payload = _payload(event, TaskFailed)
    verification = state.verifications.get(payload.task_id)
    verdict_failed = verification is not None and verification.status is VerificationStatus.FAILED
    if verification is not None and verification.status is VerificationStatus.PASSED:
        raise InvalidEventError(f"TaskFailed: checkpoint {payload.task_id!r} already passed verification")
    if (payload.failure_type is FailureType.VERIFICATION_FAILURE) != verdict_failed:
        raise InvalidEventError(
            f"TaskFailed {payload.task_id!r}: failure type VERIFICATION_FAILURE is required exactly "
            "for a checkpoint whose verification failed"
        )
    task = state.tasks.get(payload.task_id)
    refused = action_status(state, payload.task_id) if task is not None and task.action is not None else None
    expected = REFUSAL_ERROR_TYPES.get(refused or "")
    if (payload.error_type in REFUSAL_ERROR_TYPES.values() or expected is not None) and payload.error_type != expected:
        raise InvalidEventError(
            f"TaskFailed {payload.task_id!r}: error type {payload.error_type!r}; a refused action fails with "
            f"{expected!r} and only refused actions use {sorted(REFUSAL_ERROR_TYPES.values())}"
        )
    return _transition(
        state,
        payload.task_id,
        allowed_from=frozenset({TaskStatus.RUNNING}),
        to=TaskStatus.FAILED,
        event_name="TaskFailed",
        error=payload.error,
        failure=None
        if payload.failure_type is None
        else TaskFailure(
            failure_type=payload.failure_type,
            error_type=payload.error_type,
            tool_call_id=payload.tool_call_id,
            agent_id=event.agent_id,
            failed_at=event.timestamp,
        ),
        completed_at=event.timestamp,
    )


def task_cancelled(state: RunState, event: Event) -> RunState:
    payload = _payload(event, TaskCancelled)
    return _transition(
        state,
        payload.task_id,
        allowed_from=NOT_STARTED_STATUSES,
        to=TaskStatus.CANCELLED,
        event_name="TaskCancelled",
        completed_at=event.timestamp,
    )


def fact_added(state: RunState, event: Event) -> RunState:
    payload = _payload(event, FactAdded)
    if payload.fact_id in state.facts:
        raise InvalidEventError(f"fact {payload.fact_id!r} already exists")
    fact = Fact(
        fact_id=payload.fact_id,
        content=payload.content,
        source=payload.source,
        agent_id=event.agent_id,
        task_id=event.task_id,
        provenance=payload.provenance,
        claim=payload.claim,
        sequence=event.sequence,
        recorded_at=event.timestamp,
    )
    return state.model_copy(update={"facts": {**state.facts, fact.fact_id: fact}})


def tool_called(state: RunState, event: Event) -> RunState:
    payload = _payload(event, ToolCalled)
    task = state.tasks.get(event.task_id or "")
    if task is not None and task.action is not None:
        status = action_status(state, task.task_id)
        if status not in EXECUTABLE:
            raise InvalidEventError(f"ToolCalled: action {task.task_id!r} is {status}; its action may not run")
        if (payload.tool_name, payload.arguments) != (task.action.tool_name, task.action.arguments):
            raise InvalidEventError(f"ToolCalled: action {task.task_id!r} may only call exactly its approved action")
    if payload.tool_call_id in state.tool_calls:
        raise InvalidEventError(f"tool call {payload.tool_call_id!r} already exists")
    call = ToolCallState(
        tool_call_id=payload.tool_call_id,
        tool_name=payload.tool_name,
        arguments=dict(payload.arguments),
        agent_id=event.agent_id,
        task_id=event.task_id,
        sequence=event.sequence,
    )
    return state.model_copy(update={"tool_calls": {**state.tool_calls, call.tool_call_id: call}})


def _close_tool_call(state: RunState, tool_call_id: str, **changes: Any) -> RunState:
    call = state.tool_calls.get(tool_call_id)
    if call is None:
        raise InvalidEventError(f"tool call {tool_call_id!r} does not exist")
    if call.status is not ToolCallStatus.REQUESTED:
        raise InvalidEventError(f"tool call {tool_call_id!r} is already {call.status.value}")
    closed = call.model_copy(update=changes)
    return state.model_copy(update={"tool_calls": {**state.tool_calls, tool_call_id: closed}})


def tool_succeeded(state: RunState, event: Event) -> RunState:
    payload = _payload(event, ToolSucceeded)
    return _close_tool_call(
        state,
        payload.tool_call_id,
        status=ToolCallStatus.SUCCEEDED,
        output=payload.result,
        metadata=dict(payload.metadata),
    )


def tool_failed(state: RunState, event: Event) -> RunState:
    payload = _payload(event, ToolFailed)
    return _close_tool_call(
        state,
        payload.tool_call_id,
        status=ToolCallStatus.FAILED,
        error=payload.error,
        error_type=payload.error_type,
        metadata=dict(payload.metadata),
    )


def artifact_added(state: RunState, event: Event) -> RunState:
    payload = _payload(event, ArtifactAdded)
    if payload.artifact_id in state.artifacts or payload.artifact_id in state.workspace_artifacts:
        raise InvalidEventError(f"artifact {payload.artifact_id!r} already exists")
    artifact = Artifact(
        artifact_id=payload.artifact_id,
        name=payload.name,
        media_type=payload.media_type,
        content=payload.content,
        agent_id=event.agent_id,
        task_id=event.task_id,
        sequence=event.sequence,
    )
    return state.model_copy(
        update={"artifacts": {**state.artifacts, artifact.artifact_id: artifact}}
    )


def _workspace_artifact(state: RunState, artifact_id: str) -> WorkspaceArtifact:
    artifact = state.workspace_artifacts.get(artifact_id)
    if artifact is None:
        raise InvalidEventError(f"artifact {artifact_id!r} does not exist")
    return artifact


def _with_artifact(state: RunState, artifact: WorkspaceArtifact) -> RunState:
    return state.model_copy(
        update={"workspace_artifacts": {**state.workspace_artifacts, artifact.artifact_id: artifact}}
    )


def artifact_created(state: RunState, event: Event) -> RunState:
    payload = _payload(event, ArtifactCreated)
    aid = payload.artifact_id
    if aid in state.workspace_artifacts or aid in state.artifacts:
        raise InvalidEventError(f"artifact {aid!r} already exists")
    if event.task_id is None or event.task_id not in state.tasks:
        raise InvalidEventError(f"artifact {aid!r}: must be created in the context of an existing task")
    try:
        check_name(payload.name)
    except UnsafeArtifactError as exc:
        raise InvalidEventError(f"artifact {aid!r}: {exc}") from None
    for call_id in payload.tool_call_ids:
        call = state.tool_calls.get(call_id)
        if (
            call is None
            or call.task_id != event.task_id
            or call.tool_name != "artifact_write"
            or call.status is not ToolCallStatus.SUCCEEDED
        ):
            raise InvalidEventError(
                f"artifact {aid!r}: {call_id!r} is not a successful artifact_write call of task {event.task_id!r}"
            )
    missing = [f for f in payload.input_fact_ids if f not in state.facts]
    if missing:
        raise InvalidEventError(f"artifact {aid!r}: unknown input facts {missing}")
    if payload.supersedes is not None:
        _check_supersession(state, event.task_id, payload)
    return _with_artifact(state, WorkspaceArtifact(
        artifact_id=aid,
        name=payload.name,
        artifact_type=payload.artifact_type,
        agent_id=event.agent_id,
        task_id=event.task_id,
        tool_call_ids=tuple(payload.tool_call_ids),
        input_fact_ids=tuple(payload.input_fact_ids),
        supersedes=payload.supersedes,
        sequence=event.sequence,
    ))


def _check_supersession(state: RunState, task_id: str, payload: ArtifactCreated) -> None:
    """A fixed version may replace only a current artifact of the same type and name, made
    by a direct dependency of the recovery task creating it."""
    aid, old_id = payload.artifact_id, payload.supersedes
    old = state.workspace_artifacts.get(old_id or "")
    if old is None or old.artifact_type not in ("file", "project"):
        raise InvalidEventError(f"artifact {aid!r}: supersedes unknown file or project {old_id!r}")
    if (old.artifact_type, old.name) != (payload.artifact_type, payload.name):
        raise InvalidEventError(f"artifact {aid!r}: may supersede only a {payload.artifact_type} named {payload.name!r}")
    if old.status != "validated":
        raise InvalidEventError(f"artifact {aid!r}: {old_id!r} is {old.status}, not a current artifact")
    if not is_recovery_task(state, task_id):
        raise InvalidEventError(f"artifact {aid!r}: only a task created by recovery may supersede an artifact")
    if old.task_id not in {resolve_replacement(state, d) for d in state.tasks[task_id].dependencies}:
        raise InvalidEventError(f"artifact {aid!r}: {old_id!r} was not made by a direct dependency of {task_id!r}")


def artifact_file_added(state: RunState, event: Event) -> RunState:
    payload = _payload(event, ArtifactFileAdded)
    artifact = _workspace_artifact(state, payload.artifact_id)
    if artifact.status != "created" or artifact.artifact_type == "archive":
        raise InvalidEventError(f"artifact {artifact.artifact_id!r} no longer accepts files ({artifact.status})")
    if event.task_id != artifact.task_id:
        raise InvalidEventError(f"artifact {artifact.artifact_id!r} belongs to task {artifact.task_id!r}")
    try:
        if normalize_path(payload.path) != payload.path:
            raise UnsafeArtifactError(f"path {payload.path!r} is not canonical")
    except UnsafeArtifactError as exc:
        raise InvalidEventError(f"artifact {artifact.artifact_id!r}: {exc}") from None
    if any(f.path.lower() == payload.path.lower() for f in artifact.files):
        raise InvalidEventError(f"artifact {artifact.artifact_id!r} already has a file {payload.path!r}")
    if payload.tool_call_id not in artifact.tool_call_ids:
        raise InvalidEventError(
            f"artifact {artifact.artifact_id!r}: {payload.tool_call_id!r} is not one of its artifact_write calls"
        )
    file = ArtifactFile(path=payload.path, media_type=payload.media_type, size=payload.size,
                        sha256=payload.sha256, tool_call_id=payload.tool_call_id)
    return _with_artifact(state, artifact.model_copy(update={"files": (*artifact.files, file)}))


def artifact_validated(state: RunState, event: Event) -> RunState:
    payload = _payload(event, ArtifactValidated)
    artifact = _workspace_artifact(state, payload.artifact_id)
    if payload.passed:
        if artifact.status != "created":
            raise InvalidEventError(
                f"artifact {artifact.artifact_id!r} is {artifact.status}; only a created artifact can pass validation"
            )
        if payload.problems:
            raise InvalidEventError("a passed validation lists no problems")
        if (
            not artifact.files
            or payload.file_count != len(artifact.files)
            or payload.total_bytes != sum(f.size for f in artifact.files)
        ):
            raise InvalidEventError(f"artifact {artifact.artifact_id!r}: validated counts do not match its files")
        if artifact.artifact_type == "file" and len(artifact.files) != 1:
            raise InvalidEventError(f"artifact {artifact.artifact_id!r}: a file artifact has exactly one file")
        update: dict[str, Any] = {"status": "validated"}
        if artifact.artifact_type == "file":
            only = artifact.files[0]
            update.update(media_type=only.media_type, size=only.size, sha256=only.sha256)
        state = _with_artifact(state, artifact.model_copy(update=update))
        if artifact.supersedes is not None:
            state = _supersede(state, artifact.supersedes, artifact.artifact_id)
        return state
    if artifact.status not in ("created", "validated"):
        raise InvalidEventError(f"artifact {artifact.artifact_id!r} is {artifact.status}")
    if not payload.problems:
        raise InvalidEventError("a failed validation says why")
    return _with_artifact(state, artifact.model_copy(update={"status": "rejected", "problems": tuple(payload.problems)}))


def _supersede(state: RunState, old_id: str, new_id: str) -> RunState:
    """The fixed version passed validation: the old artifact (and its archive) is no longer
    current. Only status and the link change; files, checksums and provenance stay."""
    old = state.workspace_artifacts[old_id]
    if old.status != "validated":
        raise InvalidEventError(f"artifact {new_id!r}: {old_id!r} is {old.status}, not a current artifact")
    state = _with_artifact(state, old.model_copy(update={"status": "superseded", "superseded_by": new_id}))
    archive = state.workspace_artifacts.get(old.archive_id or "")
    if archive is not None:
        state = _with_artifact(state, archive.model_copy(update={"status": "superseded", "superseded_by": new_id}))
    return state


def artifact_packaged(state: RunState, event: Event) -> RunState:
    payload = _payload(event, ArtifactPackaged)
    source = _workspace_artifact(state, payload.source_artifact_id)
    if source.artifact_type != "project" or source.status != "validated" or source.archive_id is not None:
        raise InvalidEventError(f"artifact {source.artifact_id!r} is not a validated, unpackaged project")
    aid = payload.artifact_id
    if aid in state.workspace_artifacts or aid in state.artifacts:
        raise InvalidEventError(f"artifact {aid!r} already exists")
    if sorted(payload.paths) != sorted(f.path for f in source.files):
        raise InvalidEventError(f"archive {aid!r} does not contain exactly the files of project {source.artifact_id!r}")
    archive = WorkspaceArtifact(
        artifact_id=aid,
        name=payload.name,
        artifact_type="archive",
        status="validated",
        media_type=payload.media_type,
        size=payload.size,
        sha256=payload.sha256,
        source_artifact_id=source.artifact_id,
        agent_id=event.agent_id,
        task_id=source.task_id,
        tool_call_ids=source.tool_call_ids,
        input_fact_ids=source.input_fact_ids,
        sequence=event.sequence,
    )
    state = _with_artifact(state, source.model_copy(update={"archive_id": aid}))
    return _with_artifact(state, archive)


def artifact_ready(state: RunState, event: Event) -> RunState:
    payload = _payload(event, ArtifactReady)
    artifact = _workspace_artifact(state, payload.artifact_id)
    if artifact.artifact_type == "project" or artifact.status != "validated":
        raise InvalidEventError(f"artifact {artifact.artifact_id!r} is not a validated file or archive")
    if payload.sha256 != artifact.sha256:
        raise InvalidEventError(f"artifact {artifact.artifact_id!r}: checksum differs from the recorded one")
    if not run_verified(state):
        raise InvalidEventError(f"artifact {artifact.artifact_id!r}: the run's verification has not passed")
    return _with_artifact(state, artifact.model_copy(update={"status": "ready", "ready_sequence": event.sequence}))


def conflict_detected(state: RunState, event: Event) -> RunState:
    payload = _payload(event, ConflictDetected)
    cid = payload.conflict_id
    if cid in state.conflicts:
        raise InvalidEventError(f"conflict {cid!r} already exists")
    unknown = [fact_id for fact_id in payload.fact_ids if fact_id not in state.facts]
    if unknown:
        raise InvalidEventError(f"conflict {cid!r} references unknown facts {unknown}")
    conflict = ConflictState(
        conflict_id=cid,
        description=payload.description or payload.reason,
        fact_ids=tuple(payload.fact_ids),
        detected_at=event.timestamp,
        detected_sequence=event.sequence,
    )
    if payload.conflict_type is not None:
        assert payload.fact_key is not None and payload.fingerprint is not None
        if len(payload.fact_ids) < 2 or len(set(payload.fact_ids)) != len(payload.fact_ids):
            raise InvalidEventError(f"conflict {cid!r}: needs at least two distinct facts")
        facts = [state.facts[fid] for fid in payload.fact_ids]
        claims = [f.claim for f in facts if f.claim is not None]
        if len(claims) != len(facts):
            raise InvalidEventError(f"conflict {cid!r}: every fact must have a structured claim")
        if any(fact_key(c) != payload.fact_key for c in claims):
            raise InvalidEventError(f"conflict {cid!r}: facts do not all have the key {payload.fact_key}")
        actual = classify_disagreement(claims)
        if actual is not payload.conflict_type:
            raise InvalidEventError(
                f"conflict {cid!r}: facts show {actual.value if actual else 'no disagreement'}, "
                f"not {payload.conflict_type.value}"
            )
        if payload.fingerprint != conflict_fingerprint(payload.conflict_type, payload.fact_ids):
            raise InvalidEventError(f"conflict {cid!r}: fingerprint does not match its facts")
        if any(c.fingerprint == payload.fingerprint for c in state.conflicts.values()):
            raise InvalidEventError(f"conflict {cid!r}: the same conflict was already detected")
        conflict = conflict.model_copy(
            update={
                "conflict_type": payload.conflict_type,
                "fact_key": payload.fact_key,
                "fingerprint": payload.fingerprint,
            }
        )
    return state.model_copy(update={"conflicts": {**state.conflicts, cid: conflict}})


def _open_conflict(state: RunState, conflict_id: str, name: str) -> ConflictState:
    conflict = state.conflicts.get(conflict_id)
    if conflict is None:
        raise InvalidEventError(f"{name}: conflict {conflict_id!r} does not exist")
    if conflict.status is not ConflictStatus.OPEN:
        raise InvalidEventError(f"{name}: conflict {conflict_id!r} is already {conflict.status.value}")
    return conflict


def _check_resolver(state: RunState, conflict: ConflictState, resolver_task_id: str, name: str) -> None:
    if conflict.fact_key is None:
        raise InvalidEventError(f"{name}: conflict {conflict.conflict_id!r} is a free-text conflict")
    task = state.tasks.get(resolver_task_id)
    if task is None or resolution_origin(state, resolver_task_id) != conflict.conflict_id:
        raise InvalidEventError(
            f"{name}: task {resolver_task_id!r} is not a resolution task of conflict {conflict.conflict_id!r}"
        )
    if task.status is not TaskStatus.COMPLETED:
        raise InvalidEventError(f"{name}: resolver task {resolver_task_id!r} is {task.status.value}; requires completed")


def conflict_resolved(state: RunState, event: Event) -> RunState:
    payload = _payload(event, ConflictResolved)
    conflict = _open_conflict(state, payload.conflict_id, "ConflictResolved")
    if payload.resolved_fact_id is None:  # pre-Phase 6 free-text form
        if conflict.fact_key is not None:
            raise InvalidEventError(
                f"ConflictResolved: conflict {conflict.conflict_id!r} requires evidence "
                "(resolved_fact_id, evidence_fact_ids, resolver_task_id)"
            )
        resolved = conflict.model_copy(
            update={"status": ConflictStatus.RESOLVED, "resolution": payload.resolution, "resolved_at": event.timestamp}
        )
        return state.model_copy(update={"conflicts": {**state.conflicts, resolved.conflict_id: resolved}})

    assert payload.resolver_task_id is not None
    _check_resolver(state, conflict, payload.resolver_task_id, "ConflictResolved")
    usable = set(assess_evidence(state, conflict, payload.resolver_task_id).usable)
    evidence = payload.evidence_fact_ids
    if len(set(evidence)) != len(evidence) or not set(evidence) <= usable:
        raise InvalidEventError(
            f"ConflictResolved: evidence {sorted(set(evidence) - usable)} is not usable evidence "
            f"from task {payload.resolver_task_id!r}"
        )
    values = {canonical_value(state.facts[fid].claim) for fid in evidence}  # type: ignore[arg-type]
    if len(values) != 1:
        raise InvalidEventError("ConflictResolved: the evidence facts disagree")
    if payload.resolved_fact_id not in evidence:
        raise InvalidEventError("ConflictResolved: resolved_fact_id must be one of evidence_fact_ids")
    [accepted] = values
    expected = [fid for fid in conflict.fact_ids if canonical_value(state.facts[fid].claim) == accepted]  # type: ignore[arg-type]
    if sorted(payload.corroborated_fact_ids) != sorted(expected):
        raise InvalidEventError(f"ConflictResolved: corroborated_fact_ids must be {expected}")
    resolved = conflict.model_copy(
        update={
            "status": ConflictStatus.RESOLVED,
            "resolution": payload.reason,
            "resolver_task_id": payload.resolver_task_id,
            "resolved_fact_id": payload.resolved_fact_id,
            "evidence_ids": tuple(evidence),
            "corroborated_fact_ids": tuple(payload.corroborated_fact_ids),
            "resolved_at": event.timestamp,
        }
    )
    return state.model_copy(update={"conflicts": {**state.conflicts, resolved.conflict_id: resolved}})


def conflict_unresolved(state: RunState, event: Event) -> RunState:
    payload = _payload(event, ConflictUnresolved)
    conflict = _open_conflict(state, payload.conflict_id, "ConflictUnresolved")
    _check_resolver(state, conflict, payload.resolver_task_id, "ConflictUnresolved")
    foreign = [
        fid for fid in payload.evidence_fact_ids
        if fid not in state.facts or state.facts[fid].task_id != payload.resolver_task_id
    ]
    if foreign:
        raise InvalidEventError(f"ConflictUnresolved: {foreign} are not facts of task {payload.resolver_task_id!r}")
    concluded = conflict.model_copy(
        update={
            "status": ConflictStatus.UNRESOLVED,
            "resolution": payload.reason,
            "resolver_task_id": payload.resolver_task_id,
            "evidence_ids": tuple(payload.evidence_fact_ids),
            "resolved_at": event.timestamp,
        }
    )
    return state.model_copy(update={"conflicts": {**state.conflicts, concluded.conflict_id: concluded}})


def _running_verification(state: RunState, event: Event, task_id: str, name: str) -> VerificationState:
    if event.task_id != task_id:
        raise InvalidEventError(f"{name}: must be recorded in the context of task {task_id!r}")
    verification = state.verifications.get(task_id)
    if verification is None:
        raise InvalidEventError(f"{name}: task {task_id!r} is not a verification checkpoint")
    if state.tasks[task_id].status is not TaskStatus.RUNNING:
        raise InvalidEventError(f"{name}: checkpoint {task_id!r} is {state.tasks[task_id].status.value}; requires running")
    return verification


def verification_started(state: RunState, event: Event) -> RunState:
    payload = _payload(event, VerificationStarted)
    if payload.task_id is None:
        return state  # pre-Phase 7 form: recorded only
    v = _running_verification(state, event, payload.task_id, "VerificationStarted")
    if v.status is not VerificationStatus.PENDING:
        raise InvalidEventError(f"VerificationStarted: verification {v.verification_id!r} is already {v.status.value}")
    if payload.based_on_sequence != state.last_sequence:
        raise InvalidEventError(
            f"VerificationStarted: based_on_sequence {payload.based_on_sequence}, but the run is at {state.last_sequence}"
        )
    refs = context_refs(state, v.verification_id)
    recorded = {key: tuple(getattr(payload, key)) for key in refs}
    if recorded != refs or payload.semantic != v.spec.semantic:
        raise InvalidEventError("VerificationStarted: the recorded context does not match the run's state")
    started = v.model_copy(
        update={"status": VerificationStatus.RUNNING, "based_on_sequence": payload.based_on_sequence,
                "started_at": event.timestamp, **refs}
    )
    return state.model_copy(update={"verifications": {**state.verifications, v.verification_id: started}})


def _check_verdict(state: RunState, v: VerificationState, payload: VerificationPassed | VerificationFailed, name: str) -> None:
    if v.status is not VerificationStatus.RUNNING:
        raise InvalidEventError(f"{name}: verification {v.verification_id!r} is {v.status.value}; requires running")
    if tuple(payload.checks) != run_checks(state, v.verification_id):
        raise InvalidEventError(f"{name}: the recorded checks do not match the deterministic checks")
    if not v.spec.semantic and payload.semantic is not None:
        raise InvalidEventError(f"{name}: checkpoint {v.verification_id!r} does not ask for a semantic verdict")


def verification_passed(state: RunState, event: Event) -> RunState:
    payload = _payload(event, VerificationPassed)
    if payload.task_id is None:
        return state  # pre-Phase 7 form: recorded only
    v = _running_verification(state, event, payload.task_id, "VerificationPassed")
    _check_verdict(state, v, payload, "VerificationPassed")
    failing = [c.check_id for c in payload.checks if not c.passed]
    if failing:
        raise InvalidEventError(f"VerificationPassed: checks failed: {failing}")
    if v.spec.semantic and (payload.semantic is None or not payload.semantic.passed):
        raise InvalidEventError("VerificationPassed: the checkpoint requires a passing semantic verdict")
    passed = v.model_copy(
        update={"status": VerificationStatus.PASSED, "checks": tuple(payload.checks), "semantic": payload.semantic,
                "reason": payload.details, "concluded_at": event.timestamp}
    )
    return state.model_copy(update={"verifications": {**state.verifications, v.verification_id: passed}})


def verification_failed(state: RunState, event: Event) -> RunState:
    payload = _payload(event, VerificationFailed)
    if payload.task_id is None:
        return state  # pre-Phase 7 form: recorded only
    v = _running_verification(state, event, payload.task_id, "VerificationFailed")
    _check_verdict(state, v, payload, "VerificationFailed")
    checks_failed = any(not c.passed for c in payload.checks)
    if v.spec.semantic and not checks_failed and payload.semantic is None:
        raise InvalidEventError("VerificationFailed: every check passed, so the semantic verdict is required")
    if not checks_failed and (payload.semantic is None or payload.semantic.passed):
        raise InvalidEventError("VerificationFailed: no check and no semantic judgement failed")
    semantic_refs = (
        tuple(r for j in (payload.semantic.objective, *payload.semantic.constraints) if not j.passed for r in j.evidence)
        if payload.semantic is not None else ()
    )
    failed = v.model_copy(
        update={"status": VerificationStatus.FAILED, "checks": tuple(payload.checks), "semantic": payload.semantic,
                "failed_references": tuple(dict.fromkeys((*failed_references(payload.checks), *semantic_refs))),
                "reason": payload.reason, "concluded_at": event.timestamp}
    )
    return state.model_copy(update={"verifications": {**state.verifications, v.verification_id: failed}})


def policy_evaluated(state: RunState, event: Event) -> RunState:
    decision = _payload(event, PolicyEvaluated).decision
    task = state.tasks.get(decision.task_id)
    name = f"PolicyEvaluated {decision.task_id!r}"
    if task is None or task.action is None:
        raise InvalidEventError(f"{name}: not an action task")
    if event.task_id != task.task_id:
        raise InvalidEventError(f"{name}: must be recorded in the context of task {task.task_id!r}")
    if task.status is not TaskStatus.READY:
        raise InvalidEventError(f"{name}: task is {task.status.value}; the gate decides while it is ready")
    if task.task_id in state.policy:
        raise InvalidEventError(f"{name}: the task already has a policy decision")
    fingerprint = action_fingerprint(task.action.tool_name, task.action.arguments)
    if (decision.tool_name, decision.action_fingerprint) != (task.action.tool_name, fingerprint):
        raise InvalidEventError(f"{name}: the decision is not about the task's action")
    if decision.outcome is not PolicyOutcome.DENY and fingerprint in rejected_fingerprints(state):
        raise InvalidEventError(f"{name}: an identical action was rejected earlier in this run; it can only be denied")
    record = PolicyRecord(decision=decision, sequence=event.sequence, evaluated_at=event.timestamp)
    return state.model_copy(update={"policy": {**state.policy, task.task_id: record}})


def approval_requested(state: RunState, event: Event) -> RunState:
    payload = _payload(event, ApprovalRequested)
    if payload.task_id is None:
        if payload.approval_id in state.approvals:
            raise InvalidEventError(f"ApprovalRequested: approval {payload.approval_id!r} already exists")
        return state  # pre-Phase 8 form: recorded only
    name = f"ApprovalRequested {payload.approval_id!r}"
    if event.task_id != payload.task_id:
        raise InvalidEventError(f"{name}: must be recorded in the context of task {payload.task_id!r}")
    record = state.policy.get(payload.task_id)
    task = state.tasks.get(payload.task_id)
    if record is None or task is None or record.decision.outcome is not PolicyOutcome.APPROVAL_REQUIRED:
        raise InvalidEventError(f"{name}: task {payload.task_id!r} has no APPROVAL_REQUIRED decision")
    if payload.approval_id in state.approvals or approval_for_task(state, payload.task_id) is not None:
        raise InvalidEventError(f"{name}: an approval for task {payload.task_id!r} already exists")
    if payload.action != task.action:
        raise InvalidEventError(f"{name}: the action does not match the task's action")
    approval = ApprovalState(
        approval_id=payload.approval_id, task_id=payload.task_id, action=task.action, description=payload.description,
        requested_at=event.timestamp, requested_sequence=event.sequence,
    )
    return state.model_copy(update={"approvals": {**state.approvals, approval.approval_id: approval}})


def _decide_approval(state: RunState, event: Event, payload: ApprovalGranted | ApprovalRejected, status: ApprovalStatus) -> RunState:
    name = f"{payload.event_type.value} {payload.approval_id!r}"
    approval = state.approvals.get(payload.approval_id)
    if payload.task_id is None:
        if approval is not None:
            raise InvalidEventError(f"{name}: a Phase 8 approval must be decided with its task_id")
        return state  # pre-Phase 8 form: recorded only
    if approval is None:
        raise InvalidEventError(f"{name}: approval does not exist")
    if approval.task_id != payload.task_id or event.task_id != payload.task_id:
        raise InvalidEventError(f"{name}: approval belongs to task {approval.task_id!r}")
    if approval.status is not ApprovalStatus.PENDING:
        raise InvalidEventError(f"{name}: approval is already {approval.status.value}")
    decided = approval.model_copy(
        update={"status": status, "decided_at": event.timestamp, "actor": payload.actor, "decision_reason": payload.reason}
    )
    return state.model_copy(update={"approvals": {**state.approvals, decided.approval_id: decided}})


def approval_granted(state: RunState, event: Event) -> RunState:
    return _decide_approval(state, event, _payload(event, ApprovalGranted), ApprovalStatus.GRANTED)


def approval_rejected(state: RunState, event: Event) -> RunState:
    return _decide_approval(state, event, _payload(event, ApprovalRejected), ApprovalStatus.REJECTED)


def run_completed(state: RunState, event: Event) -> RunState:
    payload = _payload(event, RunCompleted)
    blockers = completion_blockers(state)
    if blockers:
        raise InvalidEventError("RunCompleted: the run cannot complete: " + "; ".join(blockers[:10]))
    return state.model_copy(
        update={"status": RunStatus.COMPLETED, "completion_summary": payload.summary}
    )


def clarification_requested(state: RunState, event: Event) -> RunState:
    """Only before anything was planned: a run with tasks has an executable plan, and a
    clarification never coexists with one. Ends the run (needs_clarification)."""
    payload = _payload(event, ClarificationRequested)
    if state.tasks:
        raise InvalidEventError(
            f"ClarificationRequested: run {state.run_id} already has {len(state.tasks)} tasks; "
            "a clarification replaces planning, it cannot follow it"
        )
    clarification = ClarificationState(
        reason=payload.reason,
        question=payload.question,
        missing=tuple(payload.missing),
        provider=payload.provider,
        model=payload.model,
        requested_at=event.timestamp,
        sequence=event.sequence,
    )
    return state.model_copy(update={"status": RunStatus.NEEDS_CLARIFICATION, "clarification": clarification})


def run_failed(state: RunState, event: Event) -> RunState:
    payload = _payload(event, RunFailed)
    return state.model_copy(update={"status": RunStatus.FAILED, "failure_reason": payload.reason})


def _check_replan_target(state: RunState, failed_task_id: str, replan_number: int, name: str) -> None:
    task = state.tasks.get(failed_task_id)
    if task is None:
        raise InvalidEventError(f"{name}: task {failed_task_id!r} does not exist")
    if task.status is not TaskStatus.FAILED:
        raise InvalidEventError(
            f"{name}: task {failed_task_id!r} is {task.status.value}; requires failed"
        )
    if task.replaced_by is not None:
        raise InvalidEventError(
            f"{name}: task {failed_task_id!r} was already replaced by {task.replaced_by!r}"
        )
    expected = state.recovery.replan_attempts + 1
    if replan_number != expected:
        raise InvalidEventError(f"{name}: replan_number {replan_number}, expected {expected}")


def _add_recovery_record(state: RunState, record: RecoveryRecord, *, accepted: bool) -> RunState:
    recovery = state.recovery
    return state.model_copy(
        update={
            "recovery": recovery.model_copy(
                update={
                    "replan_count": recovery.replan_count + int(accepted),
                    "replan_attempts": recovery.replan_attempts + 1,
                    "history": (*recovery.history, record),
                }
            )
        }
    )


def replan_triggered(state: RunState, event: Event) -> RunState:
    payload = _payload(event, ReplanTriggered)
    if payload.failed_task_id is None:
        return state  # pre-Phase 5 form: recorded, no recovery semantics
    if payload.replan_number is None or payload.failure_type is None:
        raise InvalidEventError("ReplanTriggered: replan_number and failure_type are required")
    _check_replan_target(state, payload.failed_task_id, payload.replan_number, "ReplanTriggered")
    if payload.replacement_task_id is not None and payload.replacement_task_id not in payload.new_task_ids:
        raise InvalidEventError("ReplanTriggered: replacement_task_id must be one of new_task_ids")
    record = RecoveryRecord(
        replan_number=payload.replan_number,
        outcome="accepted",
        failed_task_id=payload.failed_task_id,
        failure_type=payload.failure_type,
        summary=payload.strategy_summary or payload.reason,
        new_task_ids=tuple(payload.new_task_ids),
        replacement_task_id=payload.replacement_task_id,
        plan_fingerprint=payload.plan_fingerprint,
        sequence=event.sequence,
        recorded_at=event.timestamp,
    )
    return _add_recovery_record(state, record, accepted=True)


def replan_rejected(state: RunState, event: Event) -> RunState:
    payload = _payload(event, ReplanRejected)
    _check_replan_target(state, payload.failed_task_id, payload.replan_number, "ReplanRejected")
    record = RecoveryRecord(
        replan_number=payload.replan_number,
        outcome="rejected",
        failed_task_id=payload.failed_task_id,
        failure_type=payload.failure_type,
        summary=f"{payload.stage}: {payload.reason}",
        plan_fingerprint=payload.plan_fingerprint,
        sequence=event.sequence,
        recorded_at=event.timestamp,
    )
    return _add_recovery_record(state, record, accepted=False)


def record_only(state: RunState, event: Event) -> RunState:
    """Accept the event into history without changing derived state (later-phase semantics)."""
    return state


# RunCreated is not listed: it has no prior state and is handled by `initial_state`.
HANDLERS: dict[EventType, Handler] = {
    EventType.TASK_CREATED: task_created,
    EventType.TASK_STARTED: task_started,
    EventType.TASK_COMPLETED: task_completed,
    EventType.TASK_FAILED: task_failed,
    EventType.TASK_CANCELLED: task_cancelled,
    EventType.FACT_ADDED: fact_added,
    EventType.ARTIFACT_ADDED: artifact_added,
    EventType.ARTIFACT_CREATED: artifact_created,
    EventType.ARTIFACT_FILE_ADDED: artifact_file_added,
    EventType.ARTIFACT_VALIDATED: artifact_validated,
    EventType.ARTIFACT_PACKAGED: artifact_packaged,
    EventType.ARTIFACT_READY: artifact_ready,
    EventType.CONFLICT_DETECTED: conflict_detected,
    EventType.CONFLICT_RESOLVED: conflict_resolved,
    EventType.CONFLICT_UNRESOLVED: conflict_unresolved,
    EventType.RUN_COMPLETED: run_completed,
    EventType.RUN_FAILED: run_failed,
    EventType.CLARIFICATION_REQUESTED: clarification_requested,
    EventType.TOOL_CALLED: tool_called,
    EventType.TOOL_SUCCEEDED: tool_succeeded,
    EventType.TOOL_FAILED: tool_failed,
    EventType.REPLAN_TRIGGERED: replan_triggered,
    EventType.REPLAN_REJECTED: replan_rejected,
    EventType.VERIFICATION_STARTED: verification_started,
    EventType.VERIFICATION_PASSED: verification_passed,
    EventType.VERIFICATION_FAILED: verification_failed,
    EventType.APPROVAL_REQUESTED: approval_requested,
    EventType.APPROVAL_GRANTED: approval_granted,
    EventType.APPROVAL_REJECTED: approval_rejected,
    EventType.POLICY_EVALUATED: policy_evaluated,
}
