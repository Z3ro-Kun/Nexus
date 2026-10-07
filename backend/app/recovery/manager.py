"""RecoveryManager: what happens after a task failure has been recorded.

    Scheduler records TaskFailed (classified)
        -> RecoveryManager.handle_failure(run_id, task_id)
            1. load state; build the FailureRecord from the recorded TaskFailed
            2. RecoveryPolicy.decide: REPLAN / PROPAGATE / FAIL_RUN (deterministic)
            3. while the replan budget allows:
                 build RecoveryContext -> ReplannerAgent.replan (bounded by a timeout)
                 accepted -> append ReplanTriggered + TaskCreated... atomically; done
                 rejected -> append ReplanRejected; try again if budget remains
            4. budget spent -> FAIL_RUN (the scheduler drains running tasks, then RunFailed)
        <- RecoveryOutcome

A failed verification checkpoint (Phase 7, VERIFICATION_FAILURE) goes through the same
steps: the replanner proposes remediation tasks (`context.verification` holds the failed
checks), and the manager appends them together with a replacement checkpoint built by
code (`app.verification.checkpoint.replacement_checkpoint`: same requirements, old
dependencies plus the remediation tasks). The checkpoint then runs again once the
remediation is done.

The manager is the only writer of recovery events. It never executes tasks, never
changes existing tasks and never lets LLM output reach the event store without passing
the same validation as the original plan (plus a final projector check on append).
A failure of the replanner itself is recorded as ReplanRejected, never as a task failure,
so recovery cannot recurse.
"""

import asyncio
import logging
from uuid import UUID

from app.agents.replanner import ReplannerAgent, ReplanProposal, ReplanRejectedError
from app.core.exceptions import (
    InvalidEventError,
    LLMError,
    SchedulerContentionError,
    SequenceConflictError,
    TaskGraphError,
)
from app.core.redaction import safe_message
from app.events.base import NewEvent
from app.events.types import EventPayload, ReplanRejected, ReplanTriggered
from app.orchestration.task_graph import TaskGraph
from app.persistence.database import Database
from app.recovery.classifier import failure_record
from app.recovery.context import build_recovery_context
from app.recovery.policy import RecoveryPolicy
from app.recovery.schemas import FailureRecord, RecoveryAction, RecoveryOutcome
from app.services.runs import RunService
from app.state.models import RunState
from app.verification.checkpoint import replacement_checkpoint

logger = logging.getLogger(__name__)

REPLANNER_AGENT_ID = "replanner"
MAX_APPEND_ATTEMPTS = 50


class RecoveryManager:
    def __init__(
        self,
        database: Database,
        replanner: ReplannerAgent,
        policy: RecoveryPolicy,
        *,
        max_new_tasks: int,
        timeout_seconds: float,
    ) -> None:
        self._database = database
        self._replanner = replanner
        self._policy = policy
        self._max_new_tasks = max_new_tasks
        self._timeout = timeout_seconds

    async def handle_failure(self, run_id: UUID, task_id: str) -> RecoveryOutcome:
        state = await self._load(run_id)
        failure = failure_record(state, task_id)
        decision = self._policy.decide(failure, state)
        logger.info("recovery for task %r: %s (%s)", task_id, decision.action.value, decision.reason)
        if decision.action is not RecoveryAction.REPLAN:
            return RecoveryOutcome(action=decision.action, reason=decision.reason)

        attempts = 0
        while True:
            state = await self._load(run_id)
            decision = self._policy.decide(failure, state)
            if decision.action is not RecoveryAction.REPLAN:
                return RecoveryOutcome(action=decision.action, reason=decision.reason, attempts=attempts)
            attempts += 1
            replan_number = state.recovery.replan_attempts + 1
            outcome = await self._attempt(state, failure, replan_number)
            if outcome is not None:
                return outcome.model_copy(update={"attempts": attempts})

    async def _attempt(
        self, state: RunState, failure: FailureRecord, replan_number: int
    ) -> RecoveryOutcome | None:
        """One replanner invocation. Returns the outcome if accepted; None if rejected
        (a ReplanRejected event has then been recorded)."""
        context = build_recovery_context(
            state,
            failure,
            self._replanner.capabilities(),
            max_replans=self._policy.max_replans,
            max_new_tasks=self._max_new_tasks,
        )
        previous = {r.plan_fingerprint for r in state.recovery.history if r.plan_fingerprint}
        fingerprint: str | None = None
        try:
            proposal = await asyncio.wait_for(
                self._replanner.replan(
                    context,
                    graph=TaskGraph.from_tasks(state.tasks.values()),
                    previous_fingerprints=previous,
                ),
                self._timeout,
            )
        except ReplanRejectedError as exc:
            stage, reason, fingerprint = exc.stage, exc.message.removeprefix(f"{exc.stage}: "), exc.fingerprint
        except asyncio.TimeoutError:
            stage, reason = "timeout", f"replanner did not finish within {self._timeout:g}s"
        except LLMError as exc:
            stage, reason = "provider", f"{exc.code}: {exc.message}"
        else:
            try:
                return await self._persist(state.run_id, failure, replan_number, proposal)
            except (TaskGraphError, InvalidEventError) as exc:
                # The run changed while the replanner worked, and the plan no longer fits.
                stage, reason, fingerprint = "graph", exc.message, proposal.fingerprint

        logger.warning("replan %d for task %r rejected (%s): %s", replan_number, failure.task_id, stage, reason)
        await self._append(
            state.run_id,
            [
                ReplanRejected(
                    failed_task_id=failure.task_id,
                    failure_type=failure.failure_type,
                    replan_number=replan_number,
                    stage=stage,
                    reason=safe_message(reason, max_chars=1000),
                    plan_fingerprint=fingerprint,
                )
            ],
            failure.task_id,
        )
        return None

    async def _persist(
        self, run_id: UUID, failure: FailureRecord, replan_number: int, proposal: ReplanProposal
    ) -> RecoveryOutcome:
        """Append ReplanTriggered followed by the new TaskCreated events, atomically,
        re-validated against the run's latest state."""
        for _ in range(MAX_APPEND_ATTEMPTS):
            async with self._database.session_factory() as session:
                service = RunService(session)
                state = await service.get_state(run_id)
                if state.recovery.replan_attempts + 1 != replan_number:
                    raise InvalidEventError("another replan was recorded concurrently")
                payloads = proposal.to_task_payloads()
                replacement_task_id = proposal.replacement_task_id
                if state.tasks[failure.task_id].verification is not None:
                    checkpoint = replacement_checkpoint(state, failure.task_id, [p.task_id for p in payloads])
                    payloads.append(checkpoint)
                    replacement_task_id = checkpoint.task_id
                ordered = TaskGraph.from_tasks(state.tasks.values()).plan_additions(payloads)
                triggered = ReplanTriggered(
                    reason=safe_message(
                        f"task {failure.task_id!r} failed ({failure.failure_type.value}: "
                        f"{failure.error_type})",
                        max_chars=1000,
                    ),
                    failed_task_id=failure.task_id,
                    failure_type=failure.failure_type,
                    replan_number=replan_number,
                    strategy_summary=safe_message(proposal.strategy_summary, max_chars=1000),
                    new_task_ids=[spec.task_id for spec in ordered],
                    replacement_task_id=replacement_task_id,
                    plan_fingerprint=proposal.fingerprint,
                )
                drafts = [
                    NewEvent(payload=triggered, agent_id=REPLANNER_AGENT_ID, task_id=failure.task_id),
                    *(NewEvent(payload=spec, agent_id=REPLANNER_AGENT_ID) for spec in ordered),
                ]
                try:
                    await service.append_events(run_id, drafts, expected_sequence=state.last_sequence)
                except SequenceConflictError:
                    continue
                logger.info(
                    "replan %d for task %r accepted: %s", replan_number, failure.task_id, triggered.new_task_ids
                )
                return RecoveryOutcome(
                    action=RecoveryAction.REPLAN,
                    reason=f"replan {replan_number} accepted",
                    new_task_ids=tuple(triggered.new_task_ids),
                    replacement_task_id=replacement_task_id,
                )
        raise SchedulerContentionError(f"could not record replan after {MAX_APPEND_ATTEMPTS} attempts")

    async def _append(self, run_id: UUID, payloads: list[EventPayload], task_id: str) -> None:
        drafts = [NewEvent(payload=p, agent_id=REPLANNER_AGENT_ID, task_id=task_id) for p in payloads]
        for _ in range(MAX_APPEND_ATTEMPTS):
            async with self._database.session_factory() as session:
                try:
                    await RunService(session).append_events(run_id, drafts)
                except SequenceConflictError:
                    continue
                return
        raise SchedulerContentionError(f"could not record ReplanRejected after {MAX_APPEND_ATTEMPTS} attempts")

    async def _load(self, run_id: UUID) -> RunState:
        async with self._database.session_factory() as session:
            return await RunService(session).get_state(run_id)
