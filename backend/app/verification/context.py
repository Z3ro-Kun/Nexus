"""The verification context: everything a verifier is given about one checkpoint.

Built deterministically from projected state (never from a worker's report or the raw
event log), so the verifier sees what the run actually recorded: the objective and
constraints, the covered tasks, their facts with provenance (and whether it is valid),
artifacts, tool calls, relevant conflicts and the deterministic check results.

Worker summaries are included because they describe the work, but they are labelled as
claims: a worker saying "verified" proves nothing, and nothing in the context is an
instruction to the verifier.

Generated files (Phase 10) are shown with their checksums and, when the artifact
workspace is given, their actual text: bounded, read only for files recorded in the log
and only after the stored bytes match the recorded SHA-256 (`app.artifacts.content`).
This is what the files say, not evidence that the code runs.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.artifacts.content import FileText, read_texts
from app.artifacts.workspace import ArtifactWorkspace
from app.events.types import FactClaim, VerificationCheck, VerificationSpec
from app.state.models import RunState, TaskStatus
from app.verification.checks import context_refs, provenance_problem, run_checks

MAX_ARTIFACT_CHARS = 4000
# Generated-file text shown to the verifier: per file, and for the whole checkpoint.
MAX_GENERATED_FILE_CHARS = 12_000
MAX_GENERATED_TOTAL_CHARS = 60_000


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class CoveredTask(_Frozen):
    task_id: str
    title: str
    description: str | None
    agent_type: str | None
    task_type: str | None
    status: TaskStatus
    replaces: str | None
    # What the worker said it did. A claim, not evidence.
    worker_summary: str | None


class FactItem(_Frozen):
    fact_id: str
    task_id: str | None
    content: str
    claim: FactClaim | None
    provenance_kind: str | None
    source: str | None
    tool_call_id: str | None
    fake: bool
    # Checked by NEXUS against the run's tool calls (app.verification.checks).
    provenance_valid: bool


class ArtifactItem(_Frozen):
    artifact_id: str
    task_id: str | None
    name: str
    media_type: str
    content: str
    truncated: bool


class GeneratedFileItem(_Frozen):
    path: str
    media_type: str
    size: int
    sha256: str
    # The file's checksum-verified text (cut to the context's bounds), or None with
    # `content_note` saying why it is not shown.
    content: str | None = None
    content_truncated: bool = False
    content_note: str | None = None


class GeneratedArtifactItem(_Frozen):
    """Phase 10: a generated file or project: its manifest and its files' verified text."""

    artifact_id: str
    task_id: str | None
    name: str
    artifact_type: str
    status: str
    files: tuple[GeneratedFileItem, ...]
    archive_name: str | None


class ToolCallItem(_Frozen):
    tool_call_id: str
    task_id: str | None
    tool_name: str
    status: str
    fake: bool


class ConflictItem(_Frozen):
    conflict_id: str
    status: str
    conflict_type: str | None
    fact_key: str | None
    fact_ids: tuple[str, ...]
    accepted_fact_id: str | None
    evidence_fact_ids: tuple[str, ...]
    reason: str | None


class VerificationContext(_Frozen):
    run_id: UUID
    verification_id: str
    checkpoint_id: str
    attempt: int
    objective: str
    # The run's constraints, by index (the semantic verifier judges each one).
    constraints: tuple[str, ...]
    spec: VerificationSpec
    based_on_sequence: int
    tasks: tuple[CoveredTask, ...]
    facts: tuple[FactItem, ...]
    artifacts: tuple[ArtifactItem, ...]
    generated_artifacts: tuple[GeneratedArtifactItem, ...] = ()
    tool_calls: tuple[ToolCallItem, ...]
    conflicts: tuple[ConflictItem, ...]
    checks: tuple[VerificationCheck, ...]

    def reference_kinds(self) -> dict[str, str]:
        """id -> kind for everything the verifier may cite. On an id collision the more
        specific kind wins (fact, artifact, tool call, conflict, then task)."""
        kinds: dict[str, str] = {}
        for kind, ids in (
            ("task", [t.task_id for t in self.tasks]),
            ("conflict", [c.conflict_id for c in self.conflicts]),
            ("tool_call", [c.tool_call_id for c in self.tool_calls]),
            ("artifact", [a.artifact_id for a in self.artifacts]),
            ("artifact", [a.artifact_id for a in self.generated_artifacts]),
            ("fact", [f.fact_id for f in self.facts]),
        ):
            kinds.update(dict.fromkeys(ids, kind))
        return kinds


def build_verification_context(
    state: RunState, verification_id: str, workspace: ArtifactWorkspace | None = None
) -> VerificationContext:
    verification = state.verifications[verification_id]
    refs = context_refs(state, verification_id)
    tasks = [state.tasks[t] for t in refs["covered_task_ids"]]
    facts = [state.facts[f] for f in refs["fact_ids"]]
    artifacts = [state.artifacts[a] for a in refs["artifact_ids"]]
    calls = [state.tool_calls[c] for c in refs["tool_call_ids"]]
    conflicts = [state.conflicts[c] for c in refs["conflict_ids"]]
    covered = set(refs["covered_task_ids"])
    generated = [
        a for a in sorted(state.workspace_artifacts.values(), key=lambda a: a.sequence)
        # A superseded version is history: the verifier judges the current one.
        if a.task_id in covered and a.artifact_type != "archive" and a.status != "superseded"
    ]
    texts = read_texts(
        workspace, state.run_id, generated,
        max_file_chars=MAX_GENERATED_FILE_CHARS, max_total_chars=MAX_GENERATED_TOTAL_CHARS,
    )
    return VerificationContext(
        run_id=state.run_id,
        verification_id=verification_id,
        checkpoint_id=verification.checkpoint_id,
        attempt=verification.attempt,
        objective=verification.spec.objective,
        constraints=state.constraints,
        spec=verification.spec,
        based_on_sequence=state.last_sequence,
        tasks=tuple(
            CoveredTask(
                task_id=t.task_id, title=t.title, description=t.description, agent_type=t.agent_type,
                task_type=t.task_type, status=t.status, replaces=t.replaces, worker_summary=t.summary,
            )
            for t in tasks
        ),
        facts=tuple(
            FactItem(
                fact_id=f.fact_id, task_id=f.task_id, content=f.content, claim=f.claim,
                provenance_kind=f.provenance.kind if f.provenance else None,
                source=f.provenance.source if f.provenance else f.source,
                tool_call_id=f.provenance.tool_call_id if f.provenance else None,
                fake=bool(f.provenance and f.provenance.fake),
                provenance_valid=provenance_problem(state, f) is None,
            )
            for f in facts
        ),
        generated_artifacts=tuple(
            GeneratedArtifactItem(
                artifact_id=a.artifact_id, task_id=a.task_id, name=a.name, artifact_type=a.artifact_type,
                status=a.status,
                files=tuple(
                    _file_item(f.path, f.media_type, f.size, f.sha256, texts.get((a.artifact_id, f.path)))
                    for f in sorted(a.files, key=lambda f: f.path)
                ),
                archive_name=state.workspace_artifacts[a.archive_id].name if a.archive_id in state.workspace_artifacts else None,
            )
            for a in generated
        ),
        artifacts=tuple(
            ArtifactItem(
                artifact_id=a.artifact_id, task_id=a.task_id, name=a.name, media_type=a.media_type,
                content=a.content[:MAX_ARTIFACT_CHARS], truncated=len(a.content) > MAX_ARTIFACT_CHARS,
            )
            for a in artifacts
        ),
        tool_calls=tuple(
            ToolCallItem(
                tool_call_id=c.tool_call_id, task_id=c.task_id, tool_name=c.tool_name,
                status=c.status.value, fake=bool(c.metadata.get("fake", False)),
            )
            for c in calls
        ),
        conflicts=tuple(
            ConflictItem(
                conflict_id=c.conflict_id, status=c.status.value,
                conflict_type=c.conflict_type.value if c.conflict_type else None,
                fact_key=f"{c.fact_key.subject} / {c.fact_key.attribute}" if c.fact_key else None,
                fact_ids=c.fact_ids, accepted_fact_id=c.resolved_fact_id, evidence_fact_ids=c.evidence_ids,
                reason=c.resolution or c.description,
            )
            for c in conflicts
        ),
        checks=run_checks(state, verification_id),
    )


def _file_item(path: str, media_type: str, size: int, sha256: str, text: FileText | None) -> GeneratedFileItem:
    text = text or FileText(None, note="not shown")
    return GeneratedFileItem(
        path=path, media_type=media_type, size=size, sha256=sha256,
        content=text.content, content_truncated=text.truncated, content_note=text.note,
    )
