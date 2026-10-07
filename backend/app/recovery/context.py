"""Builds the RecoveryContext handed to the replanner. Pure: state in, context out.

The replanner sees the goal and constraints, the classified failure, the failed task, the
whole current plan (ids, types, statuses, dependencies, replacements), the results of
completed tasks (summaries and facts, bounded), the generated files and projects (their
paths, not their content), the blocked tasks, the run's earlier replan attempts, the
agents it may use with their tools, and its remaining budget.
It never sees the event log, tool outputs in full, or any credentials.
"""

import json
from collections.abc import Sequence

from app.recovery.classifier import blocked_tasks
from app.recovery.schemas import (
    AgentCapability,
    CompletedResult,
    FailedCheck,
    FailedTaskDetail,
    FailureRecord,
    GeneratedArtifactSummary,
    PreviousAttempt,
    RecoveryContext,
    TaskOverview,
    VerificationFindings,
)
from app.state.models import RunState, TaskStatus, VerificationStatus

MAX_FACTS_PER_TASK = 10
MAX_TOOL_ARGUMENT_CHARS = 500
MAX_PATHS_PER_ARTIFACT = 100


def build_recovery_context(
    state: RunState,
    failure: FailureRecord,
    agents: Sequence[AgentCapability],
    *,
    max_replans: int,
    max_new_tasks: int,
) -> RecoveryContext:
    task = state.tasks[failure.task_id]
    failed_tool = failed_args = None
    if failure.tool_call_id is not None and failure.tool_call_id in state.tool_calls:
        call = state.tool_calls[failure.tool_call_id]
        failed_tool = call.tool_name
        failed_args = json.dumps(call.arguments, ensure_ascii=False)[:MAX_TOOL_ARGUMENT_CHARS]

    ordered = sorted(state.tasks.values(), key=lambda t: t.task_id)
    replan_number = state.recovery.replan_attempts + 1
    return RecoveryContext(
        run_id=state.run_id,
        goal=state.goal,
        constraints=state.constraints,
        failure=failure,
        failed_task=FailedTaskDetail(
            task_id=task.task_id,
            title=task.title,
            description=task.description,
            agent_type=task.agent_type,
            task_type=task.task_type,
            dependencies=task.dependencies,
            failed_tool=failed_tool,
            failed_tool_arguments=failed_args,
        ),
        tasks=tuple(
            TaskOverview(
                task_id=t.task_id,
                title=t.title,
                agent_type=t.agent_type,
                task_type=t.task_type,
                status=t.status,
                dependencies=t.dependencies,
                replaces=t.replaces,
                replaced_by=t.replaced_by,
            )
            for t in ordered
        ),
        completed_results=tuple(
            CompletedResult(
                task_id=t.task_id,
                title=t.title,
                summary=t.summary,
                facts=tuple(
                    f.content for f in state.facts.values() if f.task_id == t.task_id
                )[:MAX_FACTS_PER_TASK],
            )
            for t in ordered
            if t.status is TaskStatus.COMPLETED
        ),
        blocked_tasks=tuple(blocked_tasks(state)),
        previous_attempts=tuple(
            PreviousAttempt(
                replan_number=r.replan_number,
                outcome=r.outcome,
                failed_task_id=r.failed_task_id,
                summary=r.summary,
                new_task_ids=r.new_task_ids,
            )
            for r in state.recovery.history
        ),
        agents=tuple(agents),
        replan_number=replan_number,
        replans_remaining_after_this=max(0, max_replans - replan_number),
        max_new_tasks=max_new_tasks,
        verification=verification_findings(state, failure.task_id),
        generated_artifacts=tuple(
            GeneratedArtifactSummary(
                artifact_id=a.artifact_id,
                task_id=a.task_id,
                name=a.name,
                artifact_type=a.artifact_type,
                paths=tuple(sorted(f.path for f in a.files))[:MAX_PATHS_PER_ARTIFACT],
            )
            for a in sorted(state.workspace_artifacts.values(), key=lambda a: a.sequence)
            if a.artifact_type != "archive" and a.status in ("validated", "ready")
        ),
    )


def verification_findings(state: RunState, task_id: str) -> VerificationFindings | None:
    """The failed checks of a checkpoint whose verdict failed, else None."""
    verification = state.verifications.get(task_id)
    if verification is None or verification.status is not VerificationStatus.FAILED:
        return None
    semantic = verification.semantic
    return VerificationFindings(
        checkpoint_id=verification.checkpoint_id,
        attempt=verification.attempt,
        objective=verification.spec.objective,
        covered_task_ids=verification.covered_task_ids,
        failed_checks=tuple(
            FailedCheck(check_id=c.check_id, message=c.message, references=tuple(r.id for r in c.references)[:20])
            for c in verification.checks
            if not c.passed
        ),
        semantic_failures=tuple(
            f"{j.criterion}: {j.explanation}"
            for j in ((semantic.objective, *semantic.constraints) if semantic else ())
            if not j.passed
        ),
    )
