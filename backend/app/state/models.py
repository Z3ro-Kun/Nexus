"""Materialized run state.

These models are *derived* data: they are produced only by the projector from a run's
event history and are never persisted as a source of truth. They are frozen; handlers
return new instances instead of mutating existing ones.
"""

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue

from app.events.types import (
    ActionSpec,
    ConflictType,
    Evidence,
    EvidenceRef,
    FactClaim,
    FactKey,
    FailureType,
    PolicyDecision,
    Provenance,
    SemanticVerification,
    VerificationCheck,
    VerificationSpec,
)
from app.models.run import RunStatus


class TaskStatus(str, Enum):
    """Task status as seen in projected state.

    RUNNING, COMPLETED, FAILED and CANCELLED are set by lifecycle events. PENDING, READY and
    BLOCKED apply to tasks that have not started and are derived from the dependency graph
    (see `app.orchestration.task_graph`):

    - PENDING: waiting for dependencies that have not finished yet;
    - READY: every dependency is COMPLETED, so the task may be started;
    - BLOCKED: a dependency is FAILED, CANCELLED or BLOCKED, so the task can never run.
    """

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


NOT_STARTED_STATUSES = frozenset({TaskStatus.PENDING, TaskStatus.READY, TaskStatus.BLOCKED})


class ConflictStatus(str, Enum):
    OPEN = "open"  # detected; no conclusion yet (resolution pending, failed, or not attempted)
    RESOLVED = "resolved"  # an accepted value backed by independent tool-derived evidence
    UNRESOLVED = "unresolved"  # resolution finished without a reliable result; no winner


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class TaskFailure(_Frozen):
    """Classification of a task's failure, from its TaskFailed event (Phase 5)."""

    failure_type: FailureType
    error_type: str | None = None
    tool_call_id: str | None = None
    agent_id: str | None = None
    failed_at: datetime


class TaskState(_Frozen):
    task_id: str
    title: str
    description: str | None = None
    task_type: str | None = None
    agent_type: str | None = None
    parent_id: str | None = None
    dependencies: tuple[str, ...] = ()
    status: TaskStatus = TaskStatus.PENDING
    summary: str | None = None
    error: str | None = None
    # Phase 5. None for TaskFailed events written before failures were classified.
    failure: TaskFailure | None = None
    # Phase 5 graph evolution: `replaces` is the failed task this one stands in for;
    # `replaced_by` is the task that stands in for this (failed) one.
    replaces: str | None = None
    replaced_by: str | None = None
    # Phase 6: the conflict this task was created to resolve.
    conflict_id: str | None = None
    # Phase 7: set for a verification checkpoint (agent "verifier", type "verification").
    verification: VerificationSpec | None = None
    # Phase 8: set for an action task (agent "action_executor", type "action").
    action: ActionSpec | None = None
    evidence: tuple[Evidence, ...] = ()
    result_metadata: dict[str, JsonValue] = {}
    # Timestamps of the events that caused each transition. completed_at is set when the
    # task reaches any terminal status (COMPLETED, FAILED or CANCELLED).
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class Fact(_Frozen):
    fact_id: str
    content: str
    source: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    provenance: Provenance | None = None
    # Phase 6: structured claim (None if the fact states no comparable value).
    claim: FactClaim | None = None
    sequence: int
    recorded_at: datetime


class Artifact(_Frozen):
    artifact_id: str
    name: str
    media_type: str
    content: str
    agent_id: str | None = None
    task_id: str | None = None
    sequence: int


class ArtifactFile(_Frozen):
    path: str
    media_type: str
    size: int
    sha256: str
    tool_call_id: str


class WorkspaceArtifact(_Frozen):
    """A generated file, project or archive in the artifact workspace (Phase 10). The
    bytes live in the workspace; this records what the deliverable contains, its
    checksums, where it came from and how far it got (created -> validated -> ready,
    or rejected; or validated -> superseded when a recovery task's fixed version
    replaces it, which keeps its files and checksums but is never delivered)."""

    artifact_id: str
    name: str
    artifact_type: Literal["file", "project", "archive"]
    status: Literal["created", "validated", "rejected", "ready", "superseded"] = "created"
    files: tuple[ArtifactFile, ...] = ()
    # The delivered bytes (file artifacts: the file; archives: the ZIP). None for projects.
    media_type: str | None = None
    size: int | None = None
    sha256: str | None = None
    problems: tuple[str, ...] = ()
    archive_id: str | None = None  # project -> its archive
    source_artifact_id: str | None = None  # archive -> its project
    # Supersession: the artifact this fixed version replaces / the one that replaced this.
    supersedes: str | None = None
    superseded_by: str | None = None
    # Provenance: the creating task and agent, its artifact_write calls, and the facts it
    # was given (its dependencies' results).
    agent_id: str | None = None
    task_id: str | None = None
    tool_call_ids: tuple[str, ...] = ()
    input_fact_ids: tuple[str, ...] = ()
    sequence: int
    ready_sequence: int | None = None


class ToolCallStatus(str, Enum):
    REQUESTED = "requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ToolCallState(_Frozen):
    tool_call_id: str
    tool_name: str
    arguments: dict[str, JsonValue]
    status: ToolCallStatus = ToolCallStatus.REQUESTED
    output: JsonValue = None
    error: str | None = None
    error_type: str | None = None
    metadata: dict[str, JsonValue] = {}
    agent_id: str | None = None
    task_id: str | None = None
    sequence: int


class ConflictState(_Frozen):
    conflict_id: str
    description: str | None = None
    fact_ids: tuple[str, ...] = ()
    status: ConflictStatus = ConflictStatus.OPEN
    # Legacy resolution text, or the reason of a Phase 6 resolution / non-resolution.
    resolution: str | None = None
    # Phase 6 (None for conflicts recorded in the pre-Phase 6 free-text form).
    conflict_type: ConflictType | None = None
    fact_key: FactKey | None = None
    fingerprint: str | None = None
    detected_at: datetime | None = None
    detected_sequence: int | None = None
    resolution_task_id: str | None = None  # the task created to resolve it
    resolver_task_id: str | None = None  # the task whose evidence concluded it (may be a replacement)
    resolved_fact_id: str | None = None  # the currently accepted fact (RESOLVED only)
    evidence_ids: tuple[str, ...] = ()
    corroborated_fact_ids: tuple[str, ...] = ()
    resolved_at: datetime | None = None  # when it became RESOLVED or UNRESOLVED


class RecoveryRecord(_Frozen):
    """One replanner invocation: accepted (ReplanTriggered) or rejected (ReplanRejected)."""

    replan_number: int
    outcome: Literal["accepted", "rejected"]
    failed_task_id: str
    failure_type: FailureType | None
    # accepted: the replanner's strategy summary; rejected: "<stage>: <reason>".
    summary: str
    new_task_ids: tuple[str, ...] = ()
    replacement_task_id: str | None = None
    plan_fingerprint: str | None = None
    sequence: int
    recorded_at: datetime


class RecoveryState(_Frozen):
    # Accepted replans, and all replanner invocations (accepted + rejected); the replan
    # budget (NEXUS_MAX_REPLANS_PER_RUN) is enforced on `replan_attempts`.
    replan_count: int = 0
    replan_attempts: int = 0
    history: tuple[RecoveryRecord, ...] = ()


class VerificationStatus(str, Enum):
    PENDING = "pending"  # created; its covered work is not finished yet (or it is READY)
    BLOCKED = "blocked"  # its task is BLOCKED: covered work failed and was not replaced
    RUNNING = "running"  # VerificationStarted recorded; no verdict yet
    PASSED = "passed"  # VerificationPassed
    FAILED = "failed"  # VerificationFailed (feeds Phase 5 recovery)
    ERROR = "error"  # the verifier itself could not run (task failed without a verdict)
    CANCELLED = "cancelled"


class VerificationState(_Frozen):
    """One verification attempt: a checkpoint task and, if it ran, its verdict. All of it
    is derived from TaskCreated(verification) and the Verification* events."""

    verification_id: str  # the verification task's id
    checkpoint_id: str  # the first attempt's id (replacements share it)
    attempt: int  # 1 for the original checkpoint, +1 per replacement
    spec: VerificationSpec
    dependencies: tuple[str, ...]
    status: VerificationStatus = VerificationStatus.PENDING
    # From VerificationStarted: exactly what was verified.
    based_on_sequence: int | None = None
    covered_task_ids: tuple[str, ...] = ()
    fact_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    tool_call_ids: tuple[str, ...] = ()
    conflict_ids: tuple[str, ...] = ()
    # From the verdict.
    checks: tuple[VerificationCheck, ...] = ()
    semantic: SemanticVerification | None = None
    failed_references: tuple[EvidenceRef, ...] = ()
    reason: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    concluded_at: datetime | None = None
    replaced_by: str | None = None


class PolicyRecord(_Frozen):
    """A recorded policy decision for an action task (from PolicyEvaluated)."""

    decision: PolicyDecision
    sequence: int
    evaluated_at: datetime


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    GRANTED = "granted"
    REJECTED = "rejected"


class ApprovalState(_Frozen):
    """One human approval of one action task. Derived from Approval* events only.
    GRANTED means the action may execute; whether it did is the task's status."""

    approval_id: str
    task_id: str
    action: ActionSpec
    description: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: datetime
    requested_sequence: int
    decided_at: datetime | None = None
    actor: str | None = None
    decision_reason: str | None = None


class ClarificationState(_Frozen):
    """Phase 11: why the run stopped before planning, and what to ask the user."""

    reason: Literal["underspecified", "not_a_request"]
    question: str
    missing: tuple[str, ...]
    provider: str | None = None
    model: str | None = None
    requested_at: datetime
    sequence: int


class RunState(_Frozen):
    run_id: UUID
    goal: str
    constraints: tuple[str, ...] = ()
    status: RunStatus
    tasks: dict[str, TaskState] = {}
    facts: dict[str, Fact] = {}
    artifacts: dict[str, Artifact] = {}
    # Phase 10: generated files, projects and archives in the artifact workspace.
    workspace_artifacts: dict[str, WorkspaceArtifact] = {}
    tool_calls: dict[str, ToolCallState] = {}
    conflicts: dict[str, ConflictState] = {}
    recovery: RecoveryState = RecoveryState()
    # Phase 7: verification attempts, keyed by verification task id.
    verifications: dict[str, VerificationState] = {}
    # Phase 8: policy decisions by action task id, and approvals by approval id.
    policy: dict[str, PolicyRecord] = {}
    approvals: dict[str, ApprovalState] = {}
    completion_summary: str | None = None
    failure_reason: str | None = None
    # Phase 11: set when the planner asked for clarification (status needs_clarification).
    clarification: ClarificationState | None = None
    # Position in the event log this state reflects.
    last_sequence: int
    updated_at: datetime
