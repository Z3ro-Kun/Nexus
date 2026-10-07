"""Run completion: the only writer of RunCompleted (Phase 8).

The projector accepts RunCompleted only if `app.policy.rules.completion_blockers` is
empty: every task resolved to COMPLETED, no approval pending, no refused action left
unreplaced, and the current attempt of every verification checkpoint passed. The
scheduler calls `complete_if_ready` when a pass ends; nothing else completes a run.

Delivery (Phase 10): when everything else allows completion, each validated workspace
deliverable (a file or a project archive) is re-read from the artifact workspace and
checked against its recorded SHA-256 (archives are also reopened and compared with their
manifest). All pass: ArtifactReady for each, then RunCompleted, in one append. Any fails:
ArtifactValidated(passed=False) for it and no completion, so a missing or altered file is
never delivered. Without a workspace, a run with deliverables cannot complete.
"""

import logging
from uuid import UUID

from app.core.exceptions import SchedulerContentionError, SequenceConflictError
from app.events.base import NewEvent
from app.artifacts.safety import UnsafeArtifactError
from app.artifacts.workspace import ArtifactIntegrityError, ArtifactWorkspace, verify_zip
from app.events.types import ArtifactReady, ArtifactValidated, EventPayload, RunCompleted
from app.models.run import TERMINAL_RUN_STATUSES
from app.persistence.database import Database
from app.policy.rules import completion_blockers, pending_deliveries
from app.services.runs import RunService
from app.state.models import RunState, WorkspaceArtifact
from app.verification.checkpoint import latest_attempts

logger = logging.getLogger(__name__)

COMPLETION_AGENT_ID = "run_completion"
MAX_APPEND_ATTEMPTS = 50


class RunCompletion:
    def __init__(self, database: Database, workspace: ArtifactWorkspace | None = None) -> None:
        self._database = database
        self._workspace = workspace

    async def complete_if_ready(self, run_id: UUID) -> list[str]:
        """Append RunCompleted if nothing blocks it. Returns the blockers (empty if the
        run is now completed, or already was)."""
        for _ in range(MAX_APPEND_ATTEMPTS):
            async with self._database.session_factory() as session:
                service = RunService(session)
                state = await service.get_state(run_id)
                if state.status in TERMINAL_RUN_STATUSES:
                    return [] if state.status.value == "completed" else [f"run is {state.status.value}"]
                blockers = completion_blockers(state, ignore_pending_delivery=True)
                if blockers:
                    return blockers
                if self._workspace is None and pending_deliveries(state):
                    # A configuration gap, not a bad artifact: nothing is recorded.
                    return ["deliverables cannot be checked: no artifact workspace is configured"]
                delivery, failed = self._delivery(state)
                payloads: list[EventPayload] = list(delivery)
                if not failed:
                    checkpoints = ", ".join(v.verification_id for v in latest_attempts(state))
                    summary = f"All {len(state.tasks)} tasks completed; verified by {checkpoints}."
                    payloads.append(RunCompleted(summary=summary))
                try:
                    await service.append_events(
                        run_id, [NewEvent(payload=p, agent_id=COMPLETION_AGENT_ID) for p in payloads],
                        expected_sequence=state.last_sequence,
                    )
                except SequenceConflictError:
                    continue
                if failed:
                    logger.warning("run %s: %d deliverable(s) failed the delivery check", run_id, len(failed))
                    return failed
                logger.info("run %s completed", run_id)
                return []
        raise SchedulerContentionError(f"could not record RunCompleted after {MAX_APPEND_ATTEMPTS} attempts")

    def _delivery(self, state: RunState) -> tuple[list[EventPayload], list[str]]:
        """ArtifactReady for every pending deliverable whose stored bytes check out;
        ArtifactValidated(passed=False) and a blocker for each that does not."""
        events: list[EventPayload] = []
        failed: list[str] = []
        for artifact in pending_deliveries(state):
            problem = self._recheck(state, artifact)
            if problem is None:
                events.append(ArtifactReady(artifact_id=artifact.artifact_id, sha256=artifact.sha256 or ""))
                continue
            events.append(ArtifactValidated(
                artifact_id=artifact.artifact_id, passed=False, file_count=len(artifact.files),
                total_bytes=artifact.size or 0, problems=[f"delivery check: {problem}"[:500]],
            ))
            failed.append(f"artifact {artifact.artifact_id} ({artifact.name}) failed the delivery check: {problem}")
        return events, failed

    def _recheck(self, state: RunState, artifact: WorkspaceArtifact) -> str | None:
        if self._workspace is None:
            return "no artifact workspace is configured"
        try:
            if artifact.artifact_type == "file":
                [only] = artifact.files
                self._workspace.read_file(state.run_id, artifact.task_id or "", "file", artifact.name, only.path, only.sha256)
            else:
                project = state.workspace_artifacts[artifact.source_artifact_id or ""]
                data = self._workspace.read_package(state.run_id, artifact.artifact_id, artifact.sha256 or "")
                verify_zip(data, project.name, {f.path: f.sha256 for f in project.files})
        except ArtifactIntegrityError as exc:
            return exc.message
        except (UnsafeArtifactError, KeyError, ValueError) as exc:
            return str(exc)[:300]
        return None
