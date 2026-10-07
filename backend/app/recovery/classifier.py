"""Deterministic failure classification.

    TaskExecutionResult (error_type, tool_call_id, tool events) -> FailureClassification

The runtime reports *what* went wrong as a stable `error_type`; this module maps it to a
`FailureType`, the coarse category recovery policy decides on. For tool failures the
tool's own error type (from the ToolFailed event of the failed call) decides: an
unauthorized or SSRF-blocked call is a POLICY_FAILURE, a tool timeout a TIMEOUT,
anything else a TOOL_FAILURE. Nothing here involves an LLM.
"""

from app.core.redaction import safe_message
from app.events.types import FailureType, ToolFailed
from app.orchestration.task_executor import TaskExecutionResult
from app.recovery.schemas import BlockedTask, FailureClassification, FailureRecord
from app.state.models import RunState, TaskStatus

# Tool error types (app.tools.errors) with a meaning beyond "the tool failed".
TOOL_POLICY_ERRORS = frozenset({"unauthorized", "ssrf_blocked", "policy_denied", "approval_required"})
TOOL_TIMEOUT_ERRORS = frozenset({"timeout"})

# Runtime / scheduler error types -> failure type. Unknown or missing error types are
# AGENT_FAILURE ("unclassified").
ERROR_TYPE_FAILURES: dict[str, FailureType] = {
    # the task as planned cannot run
    "unknown_agent": FailureType.PLANNING_FAILURE,
    "unsupported_task_type": FailureType.PLANNING_FAILURE,
    # time limits
    "agent_timeout": FailureType.TIMEOUT,
    "llm_timeout": FailureType.TIMEOUT,
    # the agent / model failed
    "llm_error": FailureType.AGENT_FAILURE,
    # the LLM provider refused service (HTTP 429): an availability problem, not the task's
    "llm_rate_limited": FailureType.PROVIDER_FAILURE,
    "tool_limit": FailureType.AGENT_FAILURE,
    "agent_reported_failure": FailureType.AGENT_FAILURE,
    "executor_error": FailureType.AGENT_FAILURE,
    # the agent's output failed NEXUS validation
    "llm_invalid_response": FailureType.VALIDATION_FAILURE,
    # a declined / content-filtered response; classified like any unusable response, so
    # recovery behaves as before (only the reported cause is more precise)
    "llm_content_filtered": FailureType.VALIDATION_FAILURE,
    "malformed_result": FailureType.VALIDATION_FAILURE,
    "invalid_provenance": FailureType.VALIDATION_FAILURE,
    "unrecordable_result": FailureType.VALIDATION_FAILURE,
    "result_rejected": FailureType.VALIDATION_FAILURE,
    # Phase 10: generated files failed deterministic validation (replannable remediation)
    "artifact_rejected": FailureType.VALIDATION_FAILURE,
    # the executor tried to write events it may not write
    "disallowed_events": FailureType.POLICY_FAILURE,
    # Phase 7: a verification checkpoint's verdict failed / the verifier could not run
    "verification_failed": FailureType.VERIFICATION_FAILURE,
    "verifier_timeout": FailureType.TIMEOUT,
    "verifier_unavailable": FailureType.AGENT_FAILURE,
    # Phase 8: an action task refused at the policy gate (never executed)
    "action_denied": FailureType.POLICY_FAILURE,
    "approval_rejected": FailureType.POLICY_FAILURE,
}


def classify_tool_error(tool_error_type: str | None) -> FailureType:
    if tool_error_type in TOOL_POLICY_ERRORS:
        return FailureType.POLICY_FAILURE
    if tool_error_type in TOOL_TIMEOUT_ERRORS:
        return FailureType.TIMEOUT
    return FailureType.TOOL_FAILURE


def classify_result(result: TaskExecutionResult) -> FailureClassification:
    """Classify a failed execution result. Pure and deterministic."""
    message = safe_message(result.error)
    if result.error_type == "tool_failed":
        tool_failed = next(
            (
                e
                for e in result.events
                if isinstance(e, ToolFailed) and e.tool_call_id == result.tool_call_id
            ),
            None,
        )
        tool_error = tool_failed.error_type if tool_failed is not None else None
        return FailureClassification(
            failure_type=classify_tool_error(tool_error),
            error_type=tool_error or "tool_failed",
            tool_call_id=result.tool_call_id if tool_failed is not None else None,
            message=message,
        )
    error_type = result.error_type or "unclassified"
    return FailureClassification(
        failure_type=ERROR_TYPE_FAILURES.get(error_type, FailureType.AGENT_FAILURE),
        error_type=error_type,
        message=message,
    )


def failure_record(state: RunState, task_id: str) -> FailureRecord:
    """The recorded failure of a FAILED task. TaskFailed events from before Phase 5 carry
    no classification; they are reported as an unclassified AGENT_FAILURE."""
    task = state.tasks[task_id]
    if task.status is not TaskStatus.FAILED:
        raise ValueError(f"task {task_id!r} is {task.status.value}, not failed")
    failure = task.failure
    assert task.completed_at is not None
    return FailureRecord(
        run_id=state.run_id,
        task_id=task_id,
        agent_id=failure.agent_id if failure else task.agent_type,
        failure_type=failure.failure_type if failure else FailureType.AGENT_FAILURE,
        error_type=(failure.error_type if failure else None) or "unclassified",
        message=safe_message(task.error),
        tool_call_id=failure.tool_call_id if failure else None,
        timestamp=failure.failed_at if failure else task.completed_at,
    )


def blocked_tasks(state: RunState) -> list[BlockedTask]:
    """Every BLOCKED task, classified DEPENDENCY_FAILURE, with the direct dependencies that
    block it. Blocked tasks are never replanned themselves: recovery replaces the failed
    task at the root, which unblocks them."""
    blocked = []
    for task in sorted(state.tasks.values(), key=lambda t: t.task_id):
        if task.status is not TaskStatus.BLOCKED:
            continue
        blockers = tuple(
            dep
            for dep in task.dependencies
            if state.tasks[dep].status in (TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED)
            and not _resolves_viable(state, dep)
        )
        blocked.append(
            BlockedTask(task_id=task.task_id, failure_type=FailureType.DEPENDENCY_FAILURE, blocked_by=blockers)
        )
    return blocked


def _resolves_viable(state: RunState, task_id: str) -> bool:
    """True if a failed task has a replacement chain that is not (yet) non-viable."""
    seen = set()
    while (replacement := state.tasks[task_id].replaced_by) is not None and replacement not in seen:
        seen.add(replacement)
        task_id = replacement
    return state.tasks[task_id].status not in (TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED)
