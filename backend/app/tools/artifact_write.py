"""artifact_write: the agents' only way to create deliverable files.

A call writes one or more text files into ONE artifact of the calling task, inside the
controlled workspace (`app.artifacts.workspace`):
- `project` set: files of a multi-file project (e.g. "my-project" with "src/main.py");
- `project` null: a single-file artifact (exactly one file, a plain file name).

Everything is validated before anything is written (`app.artifacts.safety`): artifact
name, relative paths, excluded paths (.env, keys, .venv, node_modules, caches, ...),
per-file / per-artifact / per-task limits, and likely secrets. A refusal writes nothing and
fails with error_type "artifact_rejected"; its message names the path and the rule, never
file content. Writing the same path again in the same task replaces it.

The tool is a side effect (category reversible_write: local, sandboxed, per run), so the
policy engine decides whether agents get it, and the tool permissions decide which agent
types may use it. It is agent-only: the planner cannot propose it as an action task.
Recorded tool events never contain file content: ToolCalled keeps each file's path, size
and SHA-256 instead (`record_arguments`); the bytes live in the workspace.
"""

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.artifacts.safety import UnsafeArtifactError, check_name, find_secrets, media_type_for, normalize_path, unique_paths
from app.artifacts.workspace import ArtifactWorkspace, Kind, sha256
from app.events.types import ActionCategory
from app.tools.errors import ToolError
from app.tools.schemas import ToolContext, ToolDefinition

TOOL_NAME = "artifact_write"
MAX_FILES_PER_CALL = 50
# Shown to the planner and replanner: how deliverable files are produced and packaged.
PLANNING_NOTE = (
    "Packaging is automatic: when a task writes a project, NEXUS validates it and packages it "
    "as a ZIP, and delivers it after verification. Never plan a task whose purpose is to "
    "package, zip, bundle, collect or copy generated files; an agent cannot read files written "
    "by tasks it does not depend on, so such a task could only write the files again from memory. "
    "Each task that writes a project produces its own complete deliverable under that project "
    "name. Prefer ONE task that writes the whole project when it is small enough for one agent. "
    "Split a project only when necessary, and then make every part depend explicitly on the task "
    "whose files it builds on (a dependent task receives those files and writes the complete, "
    "combined project), naming in each description exactly which files it writes. Independent "
    "parallel tasks must never write the same project or overlapping files. Separate, "
    "independent deliverables (different projects or files the user asked for) can still be "
    "written by parallel tasks."
)


class ArtifactRejectedError(ToolError):
    error_type = "artifact_rejected"


class ArtifactFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=300, description="Relative path inside the artifact, with '/' separators, e.g. 'src/main.py'.")
    content: str = Field(max_length=2_000_000, description="The complete text content of the file (UTF-8).")


class ArtifactWriteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str | None = Field(
        default=None, min_length=1, max_length=64,
        description="Project name (a folder name such as 'my-project') for a multi-file project; null for a single file.",
    )
    files: list[ArtifactFileInput] = Field(min_length=1, max_length=MAX_FILES_PER_CALL)


class WrittenFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    media_type: str
    size: int
    sha256: str


class ArtifactWriteOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact: str
    kind: Literal["file", "project"]
    files: list[WrittenFile]


def record_arguments(arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """What ToolCalled records: the call without file contents (size and SHA-256 instead)."""
    recorded: dict[str, JsonValue] = {k: v for k, v in arguments.items() if k != "files"}
    files = arguments.get("files")
    if isinstance(files, list):
        out: list[JsonValue] = []
        for item in files[:MAX_FILES_PER_CALL + 1]:
            if not isinstance(item, dict):
                out.append({"invalid": True})
                continue
            entry: dict[str, JsonValue] = {"path": str(item.get("path"))[:300]}
            content = item.get("content")
            if isinstance(content, str):
                data = content.encode("utf-8", errors="replace")
                entry.update(content_bytes=len(data), content_sha256=hashlib.sha256(data).hexdigest())
            else:
                entry["content_omitted"] = True
            out.append(entry)
        recorded["files"] = out
    return recorded


class ArtifactWriteTool:
    def __init__(self, workspace: ArtifactWorkspace) -> None:
        self.workspace = workspace
        limits = workspace.limits
        self.definition = ToolDefinition(
            name=TOOL_NAME,
            description=(
                "Create deliverable files for the user: a single file (project null, one file with a plain "
                "file name such as 'solution.py') or a multi-file project (project = folder name, files with "
                "relative paths such as 'src/main.py', 'tests/test_main.py', 'README.md'). NEXUS validates, "
                "packages (projects become a ZIP) and delivers them after verification."
            ),
            capabilities=(
                "Text files only, written to an isolated per-run workspace; you never see filesystem paths. "
                f"Up to {MAX_FILES_PER_CALL} files per call, {limits.max_files} files and "
                f"{limits.max_total_bytes // 1_000_000} MB per artifact, {limits.max_file_bytes // 1000} kB per file, "
                f"{limits.max_artifacts_per_task} artifacts per task; call again with the same project to add files. "
                "Deliver source only: dependency manifests (requirements.txt, package.json) rather than installed "
                "packages. Refused, and nothing written: absolute or '..' paths; .env files, keys, certificates and "
                "credentials; .venv, node_modules, .git, caches, build output and logs; content that looks like a "
                "real secret (use placeholders or environment variables). Code is not executed."
            ),
            input_model=ArtifactWriteInput,
            output_model=ArtifactWriteOutput,
            risk_level="medium",
            category=ActionCategory.REVERSIBLE_WRITE,
            timeout_seconds=30.0,
            agent_only=True,
            record_arguments=record_arguments,
            planning_note=PLANNING_NOTE,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ArtifactWriteOutput:
        assert isinstance(arguments, ArtifactWriteInput)
        try:
            kind, name, files = self._validate(arguments, context)
            written = []
            for path, data in files.items():
                self.workspace.write(context.run_id, context.task_id, kind, name, path, data)
                written.append(WrittenFile(path=path, media_type=media_type_for(path), size=len(data), sha256=sha256(data)))
        except UnsafeArtifactError as exc:
            raise ArtifactRejectedError(str(exc)) from None
        return ArtifactWriteOutput(artifact=name, kind=kind, files=written)

    def _validate(self, arguments: ArtifactWriteInput, context: ToolContext) -> tuple[Kind, str, dict[str, bytes]]:
        limits = self.workspace.limits
        kind: Kind = "project" if arguments.project is not None else "file"
        if kind == "file":
            if len(arguments.files) != 1:
                raise UnsafeArtifactError("a single-file artifact (project null) has exactly one file; set project for several")
            name = check_name(arguments.files[0].path)
        else:
            name = check_name(arguments.project or "")
        files: dict[str, bytes] = {}
        for item in arguments.files:
            path = normalize_path(item.path, limits)
            data = item.content.encode("utf-8")
            if len(data) > limits.max_file_bytes:
                raise UnsafeArtifactError(f"{path}: {len(data)} bytes; the per-file limit is {limits.max_file_bytes}")
            findings = find_secrets(path, item.content)
            if findings:
                shown = "; ".join(f.describe() for f in findings[:5])
                raise UnsafeArtifactError(
                    f"refused to write likely secrets ({shown}). Use placeholders or read them from the environment."
                )
            files[path] = data
        unique_paths(list(files))

        existing = self.workspace.existing_files(context.run_id, context.task_id, kind, name)
        if not existing and (kind, name) not in self.workspace.artifacts_of_task(context.run_id, context.task_id):
            if len(self.workspace.artifacts_of_task(context.run_id, context.task_id)) >= limits.max_artifacts_per_task:
                raise UnsafeArtifactError(f"a task can create at most {limits.max_artifacts_per_task} artifacts")
        merged = {**existing, **{p: len(d) for p, d in files.items()}}
        unique_paths(list(merged))
        if len(merged) > limits.max_files:
            raise UnsafeArtifactError(f"artifact {name!r} would have {len(merged)} files; the limit is {limits.max_files}")
        total = sum(merged.values())
        if total > limits.max_total_bytes:
            raise UnsafeArtifactError(f"artifact {name!r} would be {total} bytes; the limit is {limits.max_total_bytes}")
        return kind, name, files
