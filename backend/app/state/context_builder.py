"""Context projection: the subset of a run's state handed to a consumer.

Full state -> requested fields -> context. `run_id` and `last_sequence` are always
included so a consumer knows which run and which point in the event log its context
reflects (the sequence can be passed back as `expected_sequence` when appending).
"""

from collections.abc import Iterable
from typing import Any, Literal, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.events.types import ConflictType, FactClaim, FactKey, Provenance
from app.state.models import RunState

ContextField = Literal["goal", "constraints", "status", "tasks", "facts", "artifacts", "conflicts"]
CONTEXT_FIELDS: frozenset[str] = frozenset(get_args(ContextField))
ALWAYS_INCLUDED = frozenset({"run_id", "last_sequence"})


def build_context(state: RunState, requested_fields: Iterable[str]) -> dict[str, Any]:
    requested = set(requested_fields)
    unknown = requested - CONTEXT_FIELDS
    if unknown:
        raise ValueError(
            f"unknown context fields {sorted(unknown)}; allowed: {sorted(CONTEXT_FIELDS)}"
        )
    return state.model_dump(mode="json", include=requested | ALWAYS_INCLUDED)


# --- Task context (Phase 3) --------------------------------------------------------------


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class TaskBrief(_Frozen):
    task_id: str
    title: str
    description: str | None
    task_type: str | None
    agent_type: str | None


class FactBrief(_Frozen):
    fact_id: str
    content: str
    source: str | None
    claim: FactClaim | None = None


class ArtifactBrief(_Frozen):
    artifact_id: str
    name: str
    media_type: str
    content: str


class GeneratedFileBrief(_Frozen):
    """One file of a dependency's generated artifact. `content` is filled in by the agent
    runtime from the artifact workspace (checksum-verified, bounded); until then, and when
    it cannot be shown, it is None and `content_note` says why."""

    path: str
    media_type: str
    size: int
    sha256: str
    content: str | None = None
    content_truncated: bool = False
    content_note: str | None = None


class GeneratedArtifactBrief(_Frozen):
    """A file or project a dependency created with artifact_write (Phase 10)."""

    artifact_id: str
    task_id: str | None
    name: str
    artifact_type: str
    files: tuple[GeneratedFileBrief, ...]


class DependencyResult(_Frozen):
    task_id: str
    title: str
    summary: str | None
    facts: tuple[FactBrief, ...]
    artifacts: tuple[ArtifactBrief, ...]
    # Phase 5: set when `task_id` is a replacement standing in for this failed dependency.
    replaces: str | None = None
    # Phase 10: the dependency's validated generated files (not archives).
    generated_artifacts: tuple[GeneratedArtifactBrief, ...] = ()


class ConflictFact(_Frozen):
    fact_id: str
    content: str
    claim: FactClaim | None
    source: str | None
    provenance: Provenance | None
    task_id: str | None
    agent_id: str | None


class ConflictBrief(_Frozen):
    """What a resolution task is told about its conflict (Phase 6)."""

    conflict_id: str
    conflict_type: ConflictType | None
    fact_key: FactKey | None
    facts: tuple[ConflictFact, ...]
    objective: str


class TaskContext(_Frozen):
    """What an executor sees for one task: a projection of RunState, never the event log.

    Only the task itself and the results of its *direct* dependencies are included, so
    context size grows with the task's inputs rather than with the whole run. A failed
    dependency that was replaced (Phase 5) contributes its replacement's results. Generated
    files (Phase 10) likewise come only from direct dependencies: a sibling task never sees
    another task's workspace.
    """

    run_id: UUID
    goal: str
    constraints: tuple[str, ...]
    task: TaskBrief
    dependency_results: tuple[DependencyResult, ...]
    last_sequence: int
    # Phase 6: set for a conflict-resolution task (or a replacement of one).
    conflict: ConflictBrief | None = None
    # Set for a task created by recovery: an artifact it writes under the name of one of
    # its dependencies' generated artifacts replaces (supersedes) that one. For the
    # runtime only; not part of what the agent is shown.
    recovery_task: bool = Field(default=False, exclude=True)


def is_recovery_task(state: RunState, task_id: str) -> bool:
    """True for a task created by an accepted replan (a replacement or a remediation)."""
    return any(r.outcome == "accepted" and task_id in r.new_task_ids for r in state.recovery.history)


def resolve_replacement(state: RunState, task_id: str) -> str:
    """Follow the replacement chain from `task_id` (T1 -> T4 -> ...) to its last task."""
    seen = {task_id}
    while (replacement := state.tasks[task_id].replaced_by) is not None:
        if replacement in seen:  # pragma: no cover - the task graph rejects cycles
            break
        seen.add(replacement)
        task_id = replacement
    return task_id


def build_task_context(state: RunState, task_id: str) -> TaskContext:
    task = state.tasks[task_id]
    dependency_results = []
    for declared in task.dependencies:
        dep_id = resolve_replacement(state, declared)
        dep = state.tasks[dep_id]
        dependency_results.append(
            DependencyResult(
                task_id=dep.task_id,
                title=dep.title,
                summary=dep.summary,
                facts=tuple(
                    FactBrief(fact_id=f.fact_id, content=f.content, source=f.source, claim=f.claim)
                    for f in state.facts.values()
                    if f.task_id == dep_id
                ),
                artifacts=tuple(
                    ArtifactBrief(
                        artifact_id=a.artifact_id,
                        name=a.name,
                        media_type=a.media_type,
                        content=a.content,
                    )
                    for a in state.artifacts.values()
                    if a.task_id == dep_id
                ),
                replaces=declared if dep_id != declared else None,
                generated_artifacts=tuple(
                    GeneratedArtifactBrief(
                        artifact_id=a.artifact_id,
                        task_id=a.task_id,
                        name=a.name,
                        artifact_type=a.artifact_type,
                        files=tuple(
                            GeneratedFileBrief(path=f.path, media_type=f.media_type, size=f.size, sha256=f.sha256)
                            for f in sorted(a.files, key=lambda f: f.path)
                        ),
                    )
                    for a in sorted(state.workspace_artifacts.values(), key=lambda a: a.sequence)
                    if a.task_id == dep_id and a.artifact_type != "archive" and a.status in ("validated", "ready")
                ),
            )
        )
    return TaskContext(
        run_id=state.run_id,
        goal=state.goal,
        constraints=state.constraints,
        task=TaskBrief(
            task_id=task.task_id,
            title=task.title,
            description=task.description,
            task_type=task.task_type,
            agent_type=task.agent_type,
        ),
        dependency_results=tuple(dependency_results),
        last_sequence=state.last_sequence,
        conflict=_conflict_brief(state, task_id),
        recovery_task=is_recovery_task(state, task_id),
    )


def _conflict_brief(state: RunState, task_id: str) -> ConflictBrief | None:
    # Imported here: the detector module imports this package's models.
    from app.state.conflict_detector import resolution_origin

    conflict_id = resolution_origin(state, task_id)
    if conflict_id is None or conflict_id not in state.conflicts:
        return None
    conflict = state.conflicts[conflict_id]
    key = conflict.fact_key
    objective = (
        f"Determine the current verified {key.attribute} of {key.subject} from a source not "
        "listed below, and report it as a tool-derived fact with the same subject and attribute."
        if key
        else f"Resolve: {conflict.description}"
    )
    return ConflictBrief(
        conflict_id=conflict_id,
        conflict_type=conflict.conflict_type,
        fact_key=key,
        facts=tuple(
            ConflictFact(
                fact_id=f.fact_id,
                content=f.content,
                claim=f.claim,
                source=f.source,
                provenance=f.provenance,
                task_id=f.task_id,
                agent_id=f.agent_id,
            )
            for f in (state.facts[fid] for fid in conflict.fact_ids if fid in state.facts)
        ),
        objective=objective,
    )
