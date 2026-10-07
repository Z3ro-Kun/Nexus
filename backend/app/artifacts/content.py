"""Bounded, checksum-verified text of generated files, for model contexts.

The verifier and dependent tasks are shown what a generated file actually says, not an
agent's description of it. Only files recorded in the event log (a WorkspaceArtifact's
ArtifactFileAdded manifest) are read, always through the controlled artifact workspace,
and the stored bytes must match the recorded SHA-256 before any of it is shown. Only
UTF-8 text without NUL bytes is shown, and every context has a character budget: a file
longer than its share is cut (and marked truncated); once the budget is used up, further
files are listed without content. A file that cannot be shown says why, never which
filesystem path was involved.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.artifacts.safety import UnsafeArtifactError
from app.artifacts.workspace import ArtifactIntegrityError, ArtifactWorkspace

# Defaults; callers choose the bounds for their context.
MAX_FILE_CHARS = 12_000
MAX_TOTAL_CHARS = 60_000

NOT_TEXT = "not shown: not a UTF-8 text file"
BUDGET_USED = "not shown: the content budget of this context is used up"
INTEGRITY = "not shown: the stored bytes are missing or do not match the recorded checksum"
NO_WORKSPACE = "not shown: no artifact workspace is configured"


class RecordedFile(Protocol):
    @property
    def path(self) -> str: ...
    @property
    def sha256(self) -> str: ...


class RecordedArtifact(Protocol):
    """A generated artifact as the event log records it (e.g. a WorkspaceArtifact)."""

    @property
    def artifact_id(self) -> str: ...
    @property
    def task_id(self) -> str | None: ...
    @property
    def artifact_type(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def files(self) -> Sequence[RecordedFile]: ...


@dataclass(frozen=True)
class FileText:
    """What a context may show of one file: its (possibly truncated) text, or why not."""

    content: str | None
    truncated: bool = False
    note: str | None = None


def read_texts(
    workspace: ArtifactWorkspace | None,
    run_id: UUID,
    artifacts: Iterable[RecordedArtifact],
    *,
    max_file_chars: int = MAX_FILE_CHARS,
    max_total_chars: int = MAX_TOTAL_CHARS,
) -> dict[tuple[str, str], FileText]:
    """(artifact_id, path) -> FileText for every file of the given file/project
    artifacts (archives have no files of their own), in the given order and by path."""
    texts: dict[tuple[str, str], FileText] = {}
    remaining = max_total_chars
    for artifact in artifacts:
        if artifact.artifact_type == "archive":
            continue
        for file in sorted(artifact.files, key=lambda f: f.path):
            key = (artifact.artifact_id, file.path)
            if workspace is None:
                texts[key] = FileText(None, note=NO_WORKSPACE)
                continue
            if remaining <= 0:
                texts[key] = FileText(None, note=BUDGET_USED)
                continue
            try:
                raw = workspace.read_file(
                    run_id, artifact.task_id or "", artifact.artifact_type, artifact.name, file.path, file.sha256,  # type: ignore[arg-type]
                )
            except (ArtifactIntegrityError, UnsafeArtifactError):
                texts[key] = FileText(None, note=INTEGRITY)
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is None or "\x00" in text:
                texts[key] = FileText(None, note=NOT_TEXT)
                continue
            limit = min(max_file_chars, remaining)
            shown = text[:limit]
            remaining -= len(shown)
            texts[key] = FileText(shown, truncated=len(text) > limit)
    return texts
