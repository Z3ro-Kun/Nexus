"""Recovery data types: failure records, recovery decisions and the replanner's input.

All of these are derived from projected run state (never from the raw event log) and are
immutable. None is persisted as such; what matters is recorded as events
(TaskFailed, ReplanTriggered, ReplanRejected, TaskCreated, RunFailed).
"""

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.events.types import FailureType
from app.state.models import TaskStatus


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class FailureClassification(_Frozen):
    """What the classifier derives from a failed execution result, before it is recorded."""

    failure_type: FailureType
    error_type: str
    tool_call_id: str | None = None
    # Safe message: truncated, credentials redacted.
    message: str


class FailureRecord(_Frozen):
    """A recorded task failure, as recovery sees it (built from TaskFailed via state)."""

    run_id: UUID
    task_id: str
    agent_id: str | None
    failure_type: FailureType
    error_type: str
    message: str
    tool_call_id: str | None
    timestamp: datetime


class RecoveryAction(str, Enum):
    REPLAN = "replan"  # invoke the replanner
    PROPAGATE = "propagate"  # not recoverable: normal failure propagation (dependents blocked)
    FAIL_RUN = "fail_run"  # recoverable, but the replan budget is spent: the run fails


class RecoveryDecision(_Frozen):
    action: RecoveryAction
    reason: str


class RecoveryOutcome(_Frozen):
    """What `RecoveryManager.handle_failure` did, returned to the scheduler."""

    action: RecoveryAction
    reason: str
    # For REPLAN: the tasks that were created (dependency order) and the replacement.
    new_task_ids: tuple[str, ...] = ()
    replacement_task_id: str | None = None
    # Replanner invocations made while handling this failure (accepted + rejected).
    attempts: int = 0


# --- RecoveryContext: the replanner's input -------------------------------------------------


class TaskOverview(_Frozen):
    task_id: str
    title: str
    agent_type: str | None
    task_type: str | None
    status: TaskStatus
    dependencies: tuple[str, ...]
    replaces: str | None = None
    replaced_by: str | None = None


class FailedTaskDetail(_Frozen):
    task_id: str
    title: str
    description: str | None
    agent_type: str | None
    task_type: str | None
    dependencies: tuple[str, ...]
    # The failed tool call's name and arguments, when a tool call caused the failure.
    failed_tool: str | None = None
    failed_tool_arguments: str | None = None


class CompletedResult(_Frozen):
    task_id: str
    title: str
    summary: str | None
    facts: tuple[str, ...]


class GeneratedArtifactSummary(_Frozen):
    """Phase 10: a validated file or project a task created (manifest only: the paths).
    A remediation that fixes it depends on `task_id`, whose files it then receives."""

    artifact_id: str
    task_id: str | None
    name: str
    artifact_type: str
    paths: tuple[str, ...]


class BlockedTask(_Frozen):
    task_id: str
    failure_type: FailureType  # always DEPENDENCY_FAILURE
    blocked_by: tuple[str, ...]


class PreviousAttempt(_Frozen):
    replan_number: int
    outcome: str  # "accepted" | "rejected"
    failed_task_id: str
    summary: str
    new_task_ids: tuple[str, ...] = ()


class AgentCapability(_Frozen):
    agent_type: str
    role: str
    task_types: tuple[str, ...]
    tools: tuple[str, ...]


class FailedCheck(_Frozen):
    check_id: str
    message: str
    references: tuple[str, ...] = ()


class VerificationFindings(_Frozen):
    """Why a verification checkpoint failed (Phase 7), for remediation planning."""

    checkpoint_id: str
    attempt: int
    objective: str
    covered_task_ids: tuple[str, ...]
    failed_checks: tuple[FailedCheck, ...]
    # "<criterion>: <explanation>" for each failed semantic judgement.
    semantic_failures: tuple[str, ...] = ()


class RecoveryContext(_Frozen):
    """Everything the replanner is given. Built deterministically from state."""

    run_id: UUID
    goal: str
    constraints: tuple[str, ...]
    failure: FailureRecord
    failed_task: FailedTaskDetail
    # The current plan: every task, including failed and replaced ones.
    tasks: tuple[TaskOverview, ...]
    completed_results: tuple[CompletedResult, ...]
    blocked_tasks: tuple[BlockedTask, ...]
    previous_attempts: tuple[PreviousAttempt, ...]
    agents: tuple[AgentCapability, ...]
    replan_number: int
    replans_remaining_after_this: int
    max_new_tasks: int
    # Phase 7: set when the failed task is a verification checkpoint whose verdict failed.
    # The replanner then proposes remediation work only; NEXUS re-creates the checkpoint.
    verification: VerificationFindings | None = None
    # Phase 10: the run's generated files and projects (not archives), oldest first.
    generated_artifacts: tuple[GeneratedArtifactSummary, ...] = ()
