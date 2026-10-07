"""Agent runtime: the `TaskExecutor` that runs tasks with agents.

Scheduler -> AgentTaskExecutor -> AgentRegistry -> Agent (+ ToolSession) -> AgentResult
          <- TaskExecutionResult (tool events, fact/artifact events, summary, evidence)

The scheduler only knows the TaskExecutor interface. This executor resolves the task's
agent, gives it a per-task ToolSession (its only route to tools), runs it under a hard
timeout, validates its result and converts everything into event payloads. The agent
itself never writes anything.

- Every requested tool call becomes ToolCalled + ToolSucceeded/ToolFailed, also when the
  task fails or times out (an unfinished call is recorded as ToolFailed "interrupted").
- Fact provenance is built here from the runtime's own tool trace, not from the LLM's
  claims: a fact may say basis "tool_output" only by citing a successful tool call of this
  task, and a cited source URL must appear in that call's output.
- Workspace artifacts (Phase 10): the files a task wrote with artifact_write are taken
  from the runtime's own trace (the tool's outputs), re-read from the artifact workspace
  and validated deterministically (paths, exclusions, limits, secrets, checksums);
  projects are packaged as ZIP archives. They become ArtifactCreated / ArtifactFileAdded /
  ArtifactValidated / ArtifactPackaged events recorded with TaskCompleted. Any problem
  fails the task ("artifact_rejected"; nothing is packaged), which recovery can remediate.
  Files written with artifact_write are the deliverable: an inline report artifact that
  merely repeats one of them in full is not recorded (TaskCompleted metadata
  "inline_duplicates_omitted" names it).
- Dependency files: before the agent runs, the generated files of its direct dependencies
  (already listed in its TaskContext) get their checksum-verified, bounded text from the
  artifact workspace (`app.artifacts.content`), so a later task can build on them.
- Failures (unsupported agent, provider error, timeout, malformed or unsuccessful result,
  tool failure, tool-call limit, invalid provenance) become a failed result, recorded as
  TaskFailed. Nothing is retried. Each failure carries a stable `error_type` (see
  `ERROR_TYPES`) and, for tool failures, the failed tool call id, which the recovery
  classifier maps to a FailureType (Phase 5).
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field

from pydantic import JsonValue, ValidationError

from app.agents.registry import AgentRegistry
from app.agents.result import AgentFact, AgentResult
from app.agents.tooling import ToolCallFailedError, ToolLimitExceededError, ToolSession, ToolTraceEntry
from app.artifacts.content import read_texts
from app.artifacts.safety import UnsafeArtifactError, check_name, find_secrets, normalize_path, unique_paths
from app.artifacts.workspace import ArtifactIntegrityError, ArtifactWorkspace, sha256
from app.core.exceptions import LLMError, UnknownAgentTypeError
from app.events.types import (
    ArtifactAdded,
    ArtifactCreated,
    ArtifactFileAdded,
    ArtifactPackaged,
    ArtifactValidated,
    EventPayload,
    FactAdded,
    Provenance,
    ToolCalled,
    ToolFailed,
    ToolSucceeded,
)
from app.core.redaction import safe_message
from app.llm.schemas import describe_validation_error
from app.orchestration.task_executor import TaskExecutionResult
from app.state.context_builder import TaskContext
from app.state.models import TaskState
from app.tools.artifact_write import TOOL_NAME as ARTIFACT_TOOL, ArtifactWriteOutput, WrittenFile

logger = logging.getLogger(__name__)

# Bounds of the dependency files' text in one task's context.
MAX_DEPENDENCY_FILE_CHARS = 12_000
MAX_DEPENDENCY_TOTAL_CHARS = 40_000
# A written file shorter than this (non-whitespace chars) is too small to call an inline
# artifact containing it a duplicate.
MIN_DUPLICATE_CHARS = 40


class ProvenanceError(Exception):
    pass


# The runtime's failure causes (TaskExecutionResult.error_type). LLM error codes
# ("llm_error", "llm_timeout", "llm_invalid_response", "llm_content_filtered",
# "llm_rate_limited") are passed through as-is.
ERROR_TYPES = frozenset(
    {
        "unknown_agent",
        "unsupported_task_type",
        "agent_timeout",
        "tool_failed",
        "tool_limit",
        "malformed_result",
        "agent_reported_failure",
        "invalid_provenance",
        "unrecordable_result",
        "artifact_rejected",
    }
)


class ArtifactRejected(Exception):
    pass


@dataclass
class _Declared:
    """One artifact as the task's artifact_write calls left it (last write of a path wins)."""

    kind: str
    name: str
    files: dict[str, tuple[WrittenFile, str]] = field(default_factory=dict)  # path -> (file, tool call id)
    tool_call_ids: list[str] = field(default_factory=list)


class AgentTaskExecutor:
    def __init__(self, registry: AgentRegistry, *, timeout_seconds: float) -> None:
        self._registry = registry
        self._timeout = timeout_seconds

    async def execute(self, task: TaskState, context: TaskContext) -> TaskExecutionResult:
        agent_type = task.agent_type
        try:
            spec = self._registry.spec(agent_type or "")
            agent = self._registry.resolve(agent_type)
        except UnknownAgentTypeError as exc:
            return _failure(f"{exc.code}: {exc.message}", agent_type, error_type="unknown_agent")
        if task.task_type not in spec.task_types:
            return _failure(
                f"agent {agent_type!r} does not accept task type {task.task_type!r}",
                agent_type,
                error_type="unsupported_task_type",
            )

        session = None
        if self._registry.tool_executor is not None:
            session = ToolSession(
                self._registry.tool_executor,
                run_id=context.run_id,
                task_id=task.task_id,
                agent_type=agent_type or "",
                max_calls=self._registry.max_tool_calls,
            )
        trace = session.trace if session is not None else []
        context = self._with_dependency_files(context)

        try:
            result = await asyncio.wait_for(agent.run(context, session), self._timeout)
        except asyncio.TimeoutError:
            return _failure(
                f"agent timed out after {self._timeout:g}s", agent_type, trace, error_type="agent_timeout"
            )
        except ToolCallFailedError as exc:
            return _failure(
                str(exc), agent_type, trace, error_type="tool_failed", tool_call_id=exc.entry.tool_call_id
            )
        except ToolLimitExceededError as exc:
            return _failure(str(exc), agent_type, trace, error_type="tool_limit")
        except LLMError as exc:
            return _failure(f"{exc.code}: {exc.message}", agent_type, trace, error_type=exc.code)

        # Re-validate whatever the agent returned; an agent implementation is not trusted
        # to have produced a well-formed result.
        try:
            result = AgentResult.model_validate(
                result.model_dump() if isinstance(result, AgentResult) else result
            )
        except ValidationError as exc:
            return _failure(
                f"malformed agent result: {describe_validation_error(exc)}",
                agent_type,
                trace,
                error_type="malformed_result",
            )
        if not result.success:
            return _failure(
                f"agent reported failure: {result.error}", agent_type, trace, error_type="agent_reported_failure"
            )

        try:
            _check_evidence(result, trace)
            artifact_events, written = self._artifact_events(task, context, trace)
            kept = [a for a in result.artifacts if not _duplicates_written_file(a.content, written)]
            omitted = [a.name for a in result.artifacts if a not in kept]
            if omitted:
                result = result.model_copy(update={
                    "artifacts": kept, "metadata": {**result.metadata, "inline_duplicates_omitted": omitted},
                })
            events = [
                *tool_events(trace),
                *self._result_events(task.task_id, agent_type or "", result, trace),
                *artifact_events,
            ]
        except ProvenanceError as exc:
            return _failure(f"invalid provenance: {exc}", agent_type, trace, error_type="invalid_provenance")
        except ArtifactRejected as exc:
            return _failure(str(exc), agent_type, trace, error_type="artifact_rejected")
        except ValidationError as exc:
            return _failure(
                f"agent result could not be recorded: {describe_validation_error(exc)}",
                agent_type,
                trace,
                error_type="unrecordable_result",
            )
        return TaskExecutionResult(
            succeeded=True,
            summary=result.summary,
            events=events,
            evidence=list(result.evidence),
            metadata=dict(result.metadata),
            agent_id=agent_type,
        )

    @staticmethod
    def _result_events(
        task_id: str, agent_type: str, result: AgentResult, trace: list[ToolTraceEntry]
    ) -> list[EventPayload]:
        events: list[EventPayload] = []
        for index, fact in enumerate(result.facts, start=1):
            provenance = fact_provenance(fact, trace)
            source = provenance.source or f"{agent_type} agent ({provenance.kind.replace('_', ' ')})"
            events.append(
                FactAdded(
                    fact_id=f"{task_id}.f{index}",
                    content=fact.content,
                    source=source,
                    provenance=provenance,
                    claim=fact.claim,
                )
            )
        events.extend(
            ArtifactAdded(
                artifact_id=f"{task_id}.a{index}",
                name=artifact.name,
                media_type=artifact.media_type,
                content=artifact.content,
            )
            for index, artifact in enumerate(result.artifacts, start=1)
        )
        return events


    # --- Workspace artifacts (Phase 10) -----------------------------------------------------

    def _workspace(self) -> ArtifactWorkspace | None:
        tools = self._registry.tool_executor
        if tools is None or ARTIFACT_TOOL not in tools.registry:
            return None
        return getattr(tools.registry.get(ARTIFACT_TOOL), "workspace", None)

    def _with_dependency_files(self, context: TaskContext) -> TaskContext:
        """The context with its dependencies' generated files' verified, bounded text."""
        generated = [a for dep in context.dependency_results for a in dep.generated_artifacts]
        if not generated:
            return context
        texts = read_texts(
            self._workspace(), context.run_id, generated,
            max_file_chars=MAX_DEPENDENCY_FILE_CHARS, max_total_chars=MAX_DEPENDENCY_TOTAL_CHARS,
        )
        results = []
        for dep in context.dependency_results:
            artifacts = []
            for artifact in dep.generated_artifacts:
                files = []
                for f in artifact.files:
                    text = texts[(artifact.artifact_id, f.path)]
                    files.append(f.model_copy(update={
                        "content": text.content, "content_truncated": text.truncated, "content_note": text.note,
                    }))
                artifacts.append(artifact.model_copy(update={"files": tuple(files)}))
            results.append(dep.model_copy(update={"generated_artifacts": tuple(artifacts)}))
        return context.model_copy(update={"dependency_results": tuple(results)})

    def _artifact_events(
        self, task: TaskState, context: TaskContext, trace: list[ToolTraceEntry]
    ) -> tuple[list[EventPayload], set[str]]:
        """The task's artifact events, and the normalized texts of the files it wrote."""
        declared = _declared_artifacts(trace)
        if not declared:
            return [], set()
        workspace = self._workspace()
        if workspace is None:
            raise ArtifactRejected("the task wrote artifacts, but no artifact workspace is configured")
        inputs = [f.fact_id for dep in context.dependency_results for f in dep.facts]
        replaceable = _replaceable(context)
        events: list[EventPayload] = []
        written: set[str] = set()
        for index, artifact in enumerate(declared, start=1):
            artifact_id = f"{task.task_id}.w{index}"
            data = _validated_bytes(workspace, context, task.task_id, artifact)
            written.update(_normalized(raw.decode("utf-8")) for raw in data.values())
            paths = sorted(artifact.files)
            events.append(ArtifactCreated(
                artifact_id=artifact_id, name=artifact.name, artifact_type=artifact.kind,  # type: ignore[arg-type]
                tool_call_ids=artifact.tool_call_ids, input_fact_ids=inputs,
                supersedes=replaceable.get((artifact.kind, artifact.name)),
            ))
            events.extend(
                ArtifactFileAdded(
                    artifact_id=artifact_id, path=path, media_type=artifact.files[path][0].media_type,
                    size=artifact.files[path][0].size, sha256=artifact.files[path][0].sha256,
                    tool_call_id=artifact.files[path][1],
                )
                for path in paths
            )
            events.append(ArtifactValidated(
                artifact_id=artifact_id, passed=True, file_count=len(paths), total_bytes=sum(len(b) for b in data.values()),
            ))
            if artifact.kind == "project":
                archive_id = f"{artifact_id}.zip"
                try:
                    archive = workspace.package(context.run_id, archive_id, artifact.name, data)
                except UnsafeArtifactError as exc:
                    raise ArtifactRejected(f"artifact {artifact.name!r} could not be packaged: {exc}") from None
                events.append(ArtifactPackaged(
                    artifact_id=archive_id, source_artifact_id=artifact_id, name=f"{artifact.name}.zip",
                    size=len(archive), sha256=sha256(archive), paths=paths,
                ))
        return events, written


def _replaceable(context: TaskContext) -> dict[tuple[str, str], str]:
    """(type, name) -> the dependency artifact a recovery task's same-named artifact
    replaces. Only for tasks created by recovery, and only where exactly one direct
    dependency artifact has that type and name (ambiguity replaces nothing). The projector
    re-checks every rule."""
    if not context.recovery_task:
        return {}
    found: dict[tuple[str, str], list[str]] = {}
    for dep in context.dependency_results:
        for a in dep.generated_artifacts:
            found.setdefault((a.artifact_type, a.name), []).append(a.artifact_id)
    return {key: ids[0] for key, ids in found.items() if len(ids) == 1}


_FENCE = re.compile(r"^```[^\n]*$", re.MULTILINE)
_SPACE = re.compile(r"\s+")


def _normalized(text: str) -> str:
    """Text with code fences and all whitespace removed, for duplicate detection."""
    return _SPACE.sub("", _FENCE.sub("", text))


def _duplicates_written_file(content: str, written: set[str]) -> bool:
    """True if an inline artifact contains the complete text of a file the task wrote
    with artifact_write (ignoring whitespace and code fences)."""
    if not written:
        return False
    inline = _normalized(content)
    return any(len(text) >= MIN_DUPLICATE_CHARS and text in inline for text in written)


def _declared_artifacts(trace: list[ToolTraceEntry]) -> list[_Declared]:
    """The artifacts written by successful artifact_write calls, in order of first write.
    Built from the tool's validated outputs in the runtime's own trace, never from the
    agent's report."""
    declared: dict[tuple[str, str], _Declared] = {}
    for entry in trace:
        if entry.tool_name != ARTIFACT_TOOL or entry.result is None or not entry.result.success:
            continue
        output = ArtifactWriteOutput.model_validate(entry.result.output)
        artifact = declared.setdefault((output.kind, output.artifact), _Declared(output.kind, output.artifact))
        artifact.tool_call_ids.append(entry.tool_call_id)
        for written in output.files:
            artifact.files[written.path] = (written, entry.tool_call_id)
    return list(declared.values())


def _validated_bytes(workspace: ArtifactWorkspace, context: TaskContext, task_id: str, artifact: _Declared) -> dict[str, bytes]:
    """Re-read every declared file and re-apply every rule. Returns path -> bytes, or
    raises ArtifactRejected listing the problems (paths and rules only, never content)."""
    limits = workspace.limits
    problems: list[str] = []
    data: dict[str, bytes] = {}
    try:
        check_name(artifact.name)
        unique_paths(list(artifact.files))
    except UnsafeArtifactError as exc:
        problems.append(str(exc))
    if artifact.kind == "file" and list(artifact.files) != [artifact.name]:
        problems.append("a single-file artifact must contain exactly its own file")
    if len(artifact.files) > limits.max_files:
        problems.append(f"{len(artifact.files)} files; the limit is {limits.max_files}")
    for path, (written, _) in sorted(artifact.files.items()):
        try:
            normalize_path(path, limits)
            raw = workspace.read_file(context.run_id, task_id, artifact.kind, artifact.name, path, written.sha256)  # type: ignore[arg-type]
        except (UnsafeArtifactError, ArtifactIntegrityError) as exc:
            problems.append(f"{path}: {getattr(exc, 'message', str(exc))}")
            continue
        if len(raw) > limits.max_file_bytes:
            problems.append(f"{path}: {len(raw)} bytes; the per-file limit is {limits.max_file_bytes}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{path}: not readable as UTF-8 text")
            continue
        problems.extend(f.describe() for f in find_secrets(path, text))
        data[path] = raw
    total = sum(len(b) for b in data.values())
    if total > limits.max_total_bytes:
        problems.append(f"{total} bytes in total; the limit is {limits.max_total_bytes}")
    if problems:
        shown = "; ".join(problems[:10])
        raise ArtifactRejected(f"artifact {artifact.name!r} failed validation: {shown}")
    return data


# --- Tool trace -> events ------------------------------------------------------------------


def tool_events(trace: list[ToolTraceEntry]) -> list[EventPayload]:
    events: list[EventPayload] = []
    for entry in trace:
        events.append(
            ToolCalled(tool_call_id=entry.tool_call_id, tool_name=entry.tool_name, arguments=entry.arguments)
        )
        result = entry.result
        if result is None:
            events.append(
                ToolFailed(
                    tool_call_id=entry.tool_call_id,
                    error="interrupted before completion (the task ended while the tool was running)",
                    error_type="interrupted",
                )
            )
            continue
        metadata: dict[str, JsonValue] = {**result.metadata}
        if entry.completed_at is not None:
            metadata["completed_at"] = entry.completed_at.isoformat()
        if result.success:
            events.append(ToolSucceeded(tool_call_id=entry.tool_call_id, result=result.output, metadata=metadata))
        else:
            events.append(
                ToolFailed(
                    tool_call_id=entry.tool_call_id,
                    error=safe_message(result.error, default="tool failed"),
                    error_type=result.error_type,
                    metadata=metadata,
                )
            )
    return events


# --- Provenance ----------------------------------------------------------------------------


def _successful(trace: list[ToolTraceEntry]) -> dict[str, ToolTraceEntry]:
    return {e.tool_call_id: e for e in trace if e.result is not None and e.result.success}


def _output_urls(entry: ToolTraceEntry) -> set[str]:
    output = (entry.result.output if entry.result else None) or {}
    urls = {str(output[k]) for k in ("url", "final_url") if isinstance(output.get(k), str)}
    for item in output.get("results") or []:  # type: ignore[union-attr]
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            urls.add(item["url"])
    return urls


def _default_source(entry: ToolTraceEntry) -> str:
    output = (entry.result.output if entry.result else None) or {}
    if entry.tool_name == "http_fetch" and isinstance(output.get("final_url"), str):
        return str(output["final_url"])
    for key in ("expression", "query", "backend"):
        if isinstance(output.get(key), str):
            return f"{entry.tool_name}: {output[key]}"
    return entry.tool_name


def fact_provenance(fact: AgentFact, trace: list[ToolTraceEntry]) -> Provenance:
    if fact.basis != "tool_output":
        if fact.tool_call_id is not None or fact.source_url is not None:
            raise ProvenanceError(
                f"fact {fact.content[:60]!r} cites a tool call but has basis {fact.basis!r}"
            )
        source = "run goal and constraints" if fact.basis == "user_provided" else None
        return Provenance(kind=fact.basis, source=source)

    entry = _successful(trace).get(fact.tool_call_id or "")
    if entry is None:
        raise ProvenanceError(
            f"fact {fact.content[:60]!r} cites tool call {fact.tool_call_id!r}, "
            "which is not a successful tool call of this task"
        )
    if fact.source_url is not None and fact.source_url not in _output_urls(entry):
        raise ProvenanceError(
            f"source_url {fact.source_url!r} does not appear in the output of {entry.tool_call_id}"
        )
    assert entry.result is not None
    return Provenance(
        kind="tool_output",
        tool_name=entry.tool_name,
        tool_call_id=entry.tool_call_id,
        source=fact.source_url or _default_source(entry),
        retrieved_at=entry.completed_at,
        fake=bool(entry.result.metadata.get("fake", False)),
    )


def _check_evidence(result: AgentResult, trace: list[ToolTraceEntry]) -> None:
    successful = _successful(trace)
    for item in result.evidence:
        if item.source == "tool_output" and item.reference not in successful:
            raise ProvenanceError(
                f"evidence cites tool call {item.reference!r}, which is not a successful "
                "tool call of this task"
            )


def _failure(
    error: str,
    agent_type: str | None,
    trace: list[ToolTraceEntry] | None = None,
    *,
    error_type: str,
    tool_call_id: str | None = None,
) -> TaskExecutionResult:
    logger.warning("agent task failed (agent=%s, %s): %s", agent_type, error_type, error)
    try:
        events = tool_events(trace or [])
    except ValidationError:
        logger.exception("could not build tool events for a failed task")
        events = []
    return TaskExecutionResult(
        succeeded=False,
        error=error,
        agent_id=agent_type,
        events=events,
        error_type=error_type,
        tool_call_id=tool_call_id,
    )
