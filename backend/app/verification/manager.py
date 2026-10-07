"""VerificationManager: runs a claimed verification checkpoint; the only writer of
Phase 7 verification events.

    Scheduler claims a READY checkpoint (TaskStarted), unless a relevant conflict
    resolution is still pending (`deferral`)
        -> VerificationManager.run(run_id, task_id)
            1. append VerificationStarted with the context it was built from
            2. Verifier.verify(context): deterministic checks, then (only if they all
               pass and the spec asks for it) the LLM semantic verifier, bounded by a
               timeout
            3. re-run the deterministic checks on the latest state and append, atomically,
               VerificationPassed + TaskCompleted, or VerificationFailed + TaskFailed
               (VERIFICATION_FAILURE)
            If the verifier itself cannot run (timeout, provider error, invalid output,
            no LLM verifier configured), the checkpoint's TaskFailed carries that cause
            instead, with no verdict.
        <- VerificationOutcome; the scheduler hands a failed checkpoint to the Phase 5
           RecoveryManager like any other failed task.

The manager never executes work tasks and never edits facts, artifacts or conflicts. The
projector re-checks every verdict, so even this writer cannot record a pass that the
deterministic checks contradict.
"""

import asyncio
import logging
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.artifacts.workspace import ArtifactWorkspace
from app.core.exceptions import (
    LLMError,
    SchedulerContentionError,
    SequenceConflictError,
    VerifierUnavailableError,
)
from app.core.redaction import safe_message
from app.events.base import NewEvent
from app.events.types import (
    EventPayload,
    FailureType,
    TaskCompleted,
    TaskFailed,
    VerificationFailed,
    VerificationPassed,
    VerificationStarted,
)
from app.orchestration.task_executor import TaskExecutionResult
from app.persistence.database import Database
from app.recovery.classifier import classify_result
from app.services.runs import RunService
from app.state.models import RunState, TaskStatus, VerificationStatus
from app.verification.checks import context_refs, pending_resolution, run_checks
from app.verification.context import build_verification_context
from app.verification.verifier import VerificationResult, Verifier, result_for

logger = logging.getLogger(__name__)

VERIFIER_AGENT_ID = "verifier"
MAX_APPEND_ATTEMPTS = 50


class VerificationOutcome(BaseModel):
    """What happened to one checkpoint, returned to the scheduler."""

    model_config = ConfigDict(frozen=True)

    verification_id: str
    # True: VerificationPassed + TaskCompleted were recorded.
    passed: bool
    # The verdict, if one was recorded ("passed" / "failed"); None if the verifier could
    # not run (then `error_type` says why) or nothing was recorded.
    verdict: str | None = None
    error_type: str | None = None
    # False if the checkpoint was no longer running, so nothing was recorded.
    recorded: bool = True


class VerificationManager:
    def __init__(
        self,
        database: Database,
        verifier: Verifier,
        *,
        timeout_seconds: float,
        workspace: ArtifactWorkspace | None = None,
    ) -> None:
        self._database = database
        self._verifier = verifier
        self._timeout = timeout_seconds
        # Phase 10: where generated files are read from, so the verifier sees their text.
        self._workspace = workspace

    @staticmethod
    def deferral(state: RunState, task_id: str) -> str | None:
        """Why the checkpoint should not start yet (a pending conflict resolution), or None."""
        conflict_id = pending_resolution(state, task_id)
        return f"conflict {conflict_id} is still being resolved" if conflict_id else None

    async def run(self, run_id: UUID, task_id: str) -> VerificationOutcome:
        state = await self._start(run_id, task_id)
        if state is None:
            return VerificationOutcome(verification_id=task_id, passed=False, recorded=False)
        try:
            result = await self._verify(state, task_id)
            return await self._conclude(run_id, task_id, result)
        except asyncio.TimeoutError:
            return await self._error(run_id, task_id, f"verifier did not finish within {self._timeout:g}s", "verifier_timeout")
        except VerifierUnavailableError as exc:
            return await self._error(run_id, task_id, exc.message, "verifier_unavailable")
        except LLMError as exc:
            return await self._error(run_id, task_id, f"{exc.code}: {exc.message}", exc.code)

    async def _verify(self, state: RunState, task_id: str) -> VerificationResult:
        return await asyncio.wait_for(self._verifier.verify(build_verification_context(state, task_id, self._workspace)), self._timeout)

    async def _start(self, run_id: UUID, task_id: str) -> RunState | None:
        """Append VerificationStarted. Returns the state the context is built from."""
        for _ in range(MAX_APPEND_ATTEMPTS):
            async with self._database.session_factory() as session:
                service = RunService(session)
                state = await service.get_state(run_id)
                verification = state.verifications.get(task_id)
                if (
                    verification is None
                    or verification.status is not VerificationStatus.PENDING
                    or state.tasks[task_id].status is not TaskStatus.RUNNING
                ):
                    return None
                started = VerificationStarted(
                    verification_id=task_id,
                    task_id=task_id,
                    based_on_sequence=state.last_sequence,
                    semantic=verification.spec.semantic,
                    **{key: list(ids) for key, ids in context_refs(state, task_id).items()},
                )
                try:
                    await service.append_events(
                        run_id,
                        [NewEvent(payload=started, agent_id=VERIFIER_AGENT_ID, task_id=task_id)],
                        expected_sequence=state.last_sequence,
                    )
                except SequenceConflictError:
                    continue
                return state
        raise SchedulerContentionError(f"could not record VerificationStarted after {MAX_APPEND_ATTEMPTS} attempts")

    async def _conclude(self, run_id: UUID, task_id: str, result: VerificationResult) -> VerificationOutcome:
        """Record the verdict against the latest state: the deterministic checks are
        re-run there, so a verdict never rests on a stale view of the run."""
        for _ in range(MAX_APPEND_ATTEMPTS):
            async with self._database.session_factory() as session:
                service = RunService(session)
                state = await service.get_state(run_id)
                verification = state.verifications.get(task_id)
                if verification is None or verification.status is not VerificationStatus.RUNNING:
                    return VerificationOutcome(verification_id=task_id, passed=False, recorded=False)
                checks = run_checks(state, task_id)
                if all(c.passed for c in checks) and verification.spec.semantic and result.semantic is None:
                    # The work changed while verifying, and every check now passes: the
                    # semantic verdict has not been given yet, so verify again (once more).
                    result = await self._verify(state, task_id)
                    continue
                verdict = result_for(task_id, checks, result.semantic)
                drafts = self._verdict_events(task_id, verdict)
                try:
                    await service.append_events(run_id, drafts, expected_sequence=state.last_sequence)
                except SequenceConflictError:
                    continue
                logger.info("verification %r: %s", task_id, "passed" if verdict.passed else f"failed ({verdict.reason})")
                return VerificationOutcome(
                    verification_id=task_id, passed=verdict.passed, verdict="passed" if verdict.passed else "failed"
                )
        raise SchedulerContentionError(f"could not record the verification verdict after {MAX_APPEND_ATTEMPTS} attempts")

    @staticmethod
    def _verdict_events(task_id: str, verdict: VerificationResult) -> list[NewEvent]:
        payloads: list[EventPayload]
        if verdict.passed:
            payloads = [
                VerificationPassed(
                    verification_id=task_id, task_id=task_id, checks=list(verdict.checks),
                    semantic=verdict.semantic, details=verdict.summary,
                ),
                TaskCompleted(
                    task_id=task_id,
                    summary=f"Verification passed: {verdict.summary}"[:4000],
                    metadata={
                        "verdict": "passed",
                        "checks": len(verdict.checks),
                        "semantic": f"{verdict.semantic.provider}/{verdict.semantic.model}" if verdict.semantic else None,
                    },
                ),
            ]
        else:
            assert verdict.reason is not None
            payloads = [
                VerificationFailed(
                    verification_id=task_id, task_id=task_id, checks=list(verdict.checks),
                    semantic=verdict.semantic, reason=safe_message(verdict.reason, max_chars=2000),
                ),
                TaskFailed(
                    task_id=task_id,
                    error=safe_message(f"verification failed: {verdict.reason}"),
                    failure_type=FailureType.VERIFICATION_FAILURE,
                    error_type="verification_failed",
                ),
            ]
        return [NewEvent(payload=p, agent_id=VERIFIER_AGENT_ID, task_id=task_id) for p in payloads]

    async def _error(self, run_id: UUID, task_id: str, message: str, error_type: str) -> VerificationOutcome:
        """The verifier could not run: TaskFailed with the classified cause, no verdict."""
        classification = classify_result(TaskExecutionResult.failure(message, error_type=error_type))
        failed = TaskFailed(
            task_id=task_id,
            error=classification.message,
            failure_type=classification.failure_type,
            error_type=classification.error_type,
        )
        logger.warning("verification %r could not run (%s): %s", task_id, error_type, message)
        for _ in range(MAX_APPEND_ATTEMPTS):
            async with self._database.session_factory() as session:
                service = RunService(session)
                state = await service.get_state(run_id)
                if state.tasks[task_id].status is not TaskStatus.RUNNING:
                    return VerificationOutcome(verification_id=task_id, passed=False, recorded=False)
                try:
                    await service.append_events(
                        run_id,
                        [NewEvent(payload=failed, agent_id=VERIFIER_AGENT_ID, task_id=task_id)],
                        expected_sequence=state.last_sequence,
                    )
                except SequenceConflictError:
                    continue
                return VerificationOutcome(verification_id=task_id, passed=False, error_type=classification.error_type)
        raise SchedulerContentionError(f"could not record the verifier failure after {MAX_APPEND_ATTEMPTS} attempts")
