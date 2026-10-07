"""Artifact delivery (Phase 10): list a run's generated artifacts and download verified ones.

    GET /runs/{run_id}/artifacts                              the current workspace artifacts
                                                              (?include_superseded=true: also
                                                              versions a fix replaced)
    GET /runs/{run_id}/artifacts/{artifact_id}                one artifact's metadata
    GET /runs/{run_id}/artifacts/{artifact_id}/download       the bytes (ready only)

An artifact is looked up only in the named run's state (its event log), never by path.
Only a READY file or archive downloads: ArtifactReady is recorded by RunCompletion after
verification passed and the stored bytes were re-checked. The bytes are read from the
controlled workspace and checked against the recorded SHA-256 again on every download;
nothing on disk is served unless the log says it belongs to this deliverable. Responses
never contain workspace or filesystem paths. A project downloads as its archive.
"""

import base64
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Path, Request, Response
from pydantic import BaseModel, ConfigDict

from app.api.v1.runs import ServiceDep
from app.artifacts.safety import UnsafeArtifactError
from app.artifacts.workspace import ArtifactIntegrityError, ArtifactWorkspace
from app.core.exceptions import (
    ArtifactCorruptedError,
    ArtifactMissingError,
    ArtifactNotDeliverableError,
    ArtifactNotFoundError,
    ArtifactsDisabledError,
)
from app.state.models import RunState, WorkspaceArtifact

router = APIRouter(prefix="/runs", tags=["artifacts"])

ArtifactId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", max_length=128)]


class ArtifactFileView(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    media_type: str
    size: int
    sha256: str


class ArtifactView(BaseModel):
    """What a client needs to show and fetch a deliverable. No filesystem paths."""

    model_config = ConfigDict(frozen=True)

    artifact_id: str
    name: str
    artifact_type: Literal["file", "project", "archive"]
    status: Literal["created", "validated", "rejected", "ready", "superseded"]
    deliverable: bool
    verified: bool
    media_type: str | None
    size: int | None
    sha256: str | None
    files: list[ArtifactFileView]
    archive_id: str | None
    source_artifact_id: str | None
    # Supersession: the version this one replaced / the version that replaced this one.
    supersedes: str | None = None
    superseded_by: str | None = None
    problems: list[str]
    # Provenance
    task_id: str | None
    agent_id: str | None
    tool_call_ids: list[str]
    input_fact_ids: list[str]
    download_url: str | None


def _deliverable(artifact: WorkspaceArtifact) -> bool:
    return artifact.status == "ready" and artifact.artifact_type in ("file", "archive")


def _view(run_id: UUID, a: WorkspaceArtifact, state: RunState) -> ArtifactView:
    archive = state.workspace_artifacts.get(a.archive_id or "")
    ready = _deliverable(a) or (a.artifact_type == "project" and archive is not None and _deliverable(archive))
    return ArtifactView(
        artifact_id=a.artifact_id,
        name=a.name,
        artifact_type=a.artifact_type,
        status=a.status,
        deliverable=_deliverable(a),
        verified=ready,
        media_type=a.media_type,
        size=a.size,
        sha256=a.sha256,
        files=[ArtifactFileView(path=f.path, media_type=f.media_type, size=f.size, sha256=f.sha256) for f in a.files],
        archive_id=a.archive_id,
        source_artifact_id=a.source_artifact_id,
        supersedes=a.supersedes,
        superseded_by=a.superseded_by,
        problems=list(a.problems),
        task_id=a.task_id,
        agent_id=a.agent_id,
        tool_call_ids=list(a.tool_call_ids),
        input_fact_ids=list(a.input_fact_ids),
        download_url=f"/api/v1/runs/{run_id}/artifacts/{a.artifact_id}/download" if _deliverable(a) else None,
    )


def _find(state: RunState, artifact_id: str) -> WorkspaceArtifact:
    artifact = state.workspace_artifacts.get(artifact_id)
    if artifact is None:
        raise ArtifactNotFoundError(f"run {state.run_id} has no artifact {artifact_id!r}")
    return artifact


@router.get("/{run_id}/artifacts", response_model=list[ArtifactView])
async def list_artifacts(run_id: UUID, service: ServiceDep, include_superseded: bool = False) -> list[ArtifactView]:
    """The run's current artifacts. Versions replaced by a recovery task's fix are history:
    listed only with `include_superseded=true`, and never downloadable."""
    state = await service.get_state(run_id)
    return [
        _view(run_id, a, state) for a in sorted(state.workspace_artifacts.values(), key=lambda a: a.sequence)
        if include_superseded or a.status != "superseded"
    ]


@router.get("/{run_id}/artifacts/{artifact_id}", response_model=ArtifactView)
async def get_artifact(run_id: UUID, artifact_id: ArtifactId, service: ServiceDep) -> ArtifactView:
    state = await service.get_state(run_id)
    return _view(run_id, _find(state, artifact_id), state)


@router.get(
    "/{run_id}/artifacts/{artifact_id}/download",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}, "description": "The artifact's bytes."}},
)
async def download_artifact(run_id: UUID, artifact_id: ArtifactId, request: Request, service: ServiceDep) -> Response:
    state = await service.get_state(run_id)
    artifact = _find(state, artifact_id)
    if artifact.artifact_type == "project":
        hint = f"; download its archive {artifact.archive_id!r}" if artifact.archive_id else ""
        raise ArtifactNotDeliverableError(f"artifact {artifact_id!r} is a project{hint}")
    if not _deliverable(artifact):
        raise ArtifactNotDeliverableError(f"artifact {artifact_id!r} is {artifact.status}, not verified and ready for delivery")
    workspace: ArtifactWorkspace | None = getattr(request.app.state, "artifact_workspace", None)
    if workspace is None:
        raise ArtifactsDisabledError("no artifact workspace is configured")
    try:
        if artifact.artifact_type == "file":
            [only] = artifact.files
            data = workspace.read_file(run_id, artifact.task_id or "", "file", artifact.name, only.path, only.sha256)
        else:
            data = workspace.read_package(run_id, artifact.artifact_id, artifact.sha256 or "")
    except ArtifactIntegrityError as exc:
        if exc.missing:
            raise ArtifactMissingError(f"artifact {artifact_id!r} is no longer stored") from None
        raise ArtifactCorruptedError(f"artifact {artifact_id!r} does not match its recorded checksum") from None
    except UnsafeArtifactError:
        raise ArtifactCorruptedError(f"artifact {artifact_id!r} cannot be located safely") from None
    media_type = artifact.media_type or "application/octet-stream"
    if media_type.startswith("text/") or media_type in ("application/json", "application/toml", "application/yaml"):
        media_type += "; charset=utf-8"
    return Response(
        content=data,
        media_type=media_type,
        headers={
            # Always a download, never rendered at the API's origin.
            "Content-Disposition": f'attachment; filename="{artifact.name}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, no-store",
            "Digest": "sha-256=" + base64.b64encode(bytes.fromhex(artifact.sha256 or "")).decode(),
            "X-Artifact-SHA256": artifact.sha256 or "",
        },
    )
