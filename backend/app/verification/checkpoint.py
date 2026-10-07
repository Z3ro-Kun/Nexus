"""Building verification checkpoints (TaskCreated payloads). Pure.

A checkpoint is an ordinary task in the graph (agent "verifier", type "verification")
carrying a VerificationSpec. Its dependencies are the verification boundary: the
scheduler can start it only when they have completed (through Phase 5 replacements), so
it can never run before the work it verifies, and it can never verify itself.
"""

from collections.abc import Sequence

from app.events.types import (
    VERIFICATION_TASK_TYPE,
    VERIFIER_AGENT_TYPE,
    TaskCreated,
    VerificationSpec,
)
from app.state.conflict_detector import resolution_origin
from app.state.models import RunState, TaskStatus, VerificationState, VerificationStatus


def default_scope(state: RunState) -> list[str]:
    """Every current work task: not a checkpoint, not a conflict-resolution task, and not
    a failed task that has been replaced (its replacement stands for it)."""
    return sorted(
        t.task_id
        for t in state.tasks.values()
        if t.verification is None
        and resolution_origin(state, t.task_id) is None
        and not (t.status is TaskStatus.FAILED and t.replaced_by is not None)
    )


def checkpoint_task(
    state: RunState,
    *,
    task_id: str,
    spec: VerificationSpec,
    dependencies: Sequence[str] | None = None,
    title: str | None = None,
) -> TaskCreated:
    """A new checkpoint over `dependencies` (default: `default_scope`). The task graph
    and the projector validate it when it is recorded."""
    return TaskCreated(
        task_id=task_id,
        title=title or "Verify the objective",
        description=f"Independent verification: {spec.objective}"[:2000],
        task_type=VERIFICATION_TASK_TYPE,
        agent_type=VERIFIER_AGENT_TYPE,
        dependencies=list(dependencies if dependencies is not None else default_scope(state)),
        verification=spec,
    )


def latest_attempts(state: RunState) -> list[VerificationState]:
    """The current attempt of every checkpoint (the end of each replacement chain)."""
    return sorted(
        (v for v in state.verifications.values() if v.replaced_by is None),
        key=lambda v: (v.checkpoint_id, v.attempt),
    )


def run_verified(state: RunState) -> bool:
    """True if the run has a checkpoint and the current attempt of every checkpoint passed."""
    latest = latest_attempts(state)
    return bool(latest) and all(v.status is VerificationStatus.PASSED for v in latest)


def replacement_checkpoint(state: RunState, failed_id: str, remediation_task_ids: Sequence[str]) -> TaskCreated:
    """The checkpoint that replaces a failed one after a recovery replan: the same spec,
    the same dependencies plus the remediation tasks. Built by code, never by an LLM."""
    failed = state.tasks[failed_id]
    verification = state.verifications[failed_id]
    assert failed.verification is not None
    attempt = verification.attempt + 1
    base = f"{verification.checkpoint_id}_attempt{attempt}"
    task_id, n = base[:120], 1
    while task_id in state.tasks:
        n += 1
        task_id = f"{base[:115]}_{n}"
    dependencies = list(dict.fromkeys([*failed.dependencies, *remediation_task_ids]))
    root_title = state.tasks[verification.checkpoint_id].title
    return TaskCreated(
        task_id=task_id,
        title=f"{root_title} (attempt {attempt})"[:200],
        description=failed.description,
        task_type=VERIFICATION_TASK_TYPE,
        agent_type=VERIFIER_AGENT_TYPE,
        dependencies=dependencies,
        replaces=failed_id,
        verification=failed.verification,
    )
