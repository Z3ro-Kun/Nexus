"""Deterministic recovery policy: may this failure be replanned?

    FailureRecord + RunState -> RecoveryDecision (REPLAN | PROPAGATE | FAIL_RUN)

The LLM is never asked. Rules, in order:

1. the run is already terminal                     -> PROPAGATE
   an LLM provider refused service (PROVIDER_FAILURE, -> FAIL_RUN
   e.g. HTTP 429): no plan can fix that, so the run
   fails with the provider reason; no replan, no
   budget spent
   a verification checkpoint failed without a       -> PROPAGATE
   failed verdict (the verifier itself could not
   run: replanning work cannot fix that)
2. the failure type is not recoverable             -> PROPAGATE
   (except a human's approval rejection, POLICY_FAILURE / approval_rejected:
    a different route may be planned, never the same action again)
   (POLICY_FAILURE: a refused action must not be routed around by replanning;
    DEPENDENCY_FAILURE: the root failure is what gets recovered)
3. the error type is an internal error             -> PROPAGATE
   ("executor_error": an exception inside NEXUS, i.e. a bug or malformed state)
4. the task already has a replacement              -> PROPAGATE
5. the run's replan budget is spent                -> FAIL_RUN
6. otherwise                                       -> REPLAN

VERIFICATION_FAILURE (Phase 7: a checkpoint's verdict failed) is recoverable: the
replanner proposes remediation work and the recovery manager re-creates the checkpoint
over it (see app.recovery.manager).

The budget (`max_replans`, NEXUS_MAX_REPLANS_PER_RUN) counts every replanner invocation in
the run, accepted or rejected, so a replanner that keeps producing invalid or repeated
plans also exhausts it.
"""

from app.events.types import FailureType
from app.models.run import TERMINAL_RUN_STATUSES
from app.recovery.schemas import FailureRecord, RecoveryAction, RecoveryDecision
from app.state.models import RunState

RECOVERABLE_FAILURES = frozenset(
    {
        FailureType.TOOL_FAILURE,
        FailureType.AGENT_FAILURE,
        FailureType.TIMEOUT,
        FailureType.VALIDATION_FAILURE,
        FailureType.PLANNING_FAILURE,
        FailureType.VERIFICATION_FAILURE,
    }
)
NON_RECOVERABLE_ERROR_TYPES = frozenset({"executor_error"})
# Phase 8: a human rejected the action. Replanning may propose another route (the
# replanner cannot create action tasks, and an identical action would be denied); a
# policy DENY (action_denied) is never routed around.
RECOVERABLE_POLICY_ERROR_TYPES = frozenset({"approval_rejected"})


class RecoveryPolicy:
    def __init__(self, max_replans: int) -> None:
        if max_replans < 0:
            raise ValueError("max_replans must be >= 0")
        self.max_replans = max_replans

    def remaining(self, state: RunState) -> int:
        return max(0, self.max_replans - state.recovery.replan_attempts)

    def decide(self, failure: FailureRecord, state: RunState) -> RecoveryDecision:
        if state.status in TERMINAL_RUN_STATUSES:
            return _propagate(f"run is {state.status.value}")
        if failure.failure_type is FailureType.PROVIDER_FAILURE:
            return RecoveryDecision(
                action=RecoveryAction.FAIL_RUN,
                reason=(
                    f"LLM provider unavailable: task {failure.task_id!r} failed ({failure.error_type}): "
                    f"{failure.message} Not replanned: a different plan cannot fix a provider rate limit."
                ),
            )
        task = state.tasks[failure.task_id]
        if task.verification is not None and failure.failure_type is not FailureType.VERIFICATION_FAILURE:
            return _propagate(
                f"verification checkpoint {failure.task_id!r} could not run ({failure.failure_type.value}: "
                f"{failure.error_type}); only a failed verdict is replanned"
            )
        if failure.failure_type not in RECOVERABLE_FAILURES and not (
            failure.failure_type is FailureType.POLICY_FAILURE and failure.error_type in RECOVERABLE_POLICY_ERROR_TYPES
        ):
            return _propagate(f"{failure.failure_type.value} is not eligible for replanning")
        if failure.error_type in NON_RECOVERABLE_ERROR_TYPES:
            return _propagate(f"internal error ({failure.error_type}) is not eligible for replanning")
        if task.replaced_by is not None:
            return _propagate(f"task {failure.task_id!r} already has replacement {task.replaced_by!r}")
        if self.remaining(state) == 0:
            return RecoveryDecision(
                action=RecoveryAction.FAIL_RUN,
                reason=(
                    f"replan budget exhausted ({state.recovery.replan_attempts} of "
                    f"{self.max_replans} replans used); task {failure.task_id!r} failed "
                    f"({failure.failure_type.value}: {failure.error_type})"
                ),
            )
        return RecoveryDecision(
            action=RecoveryAction.REPLAN,
            reason=f"{failure.failure_type.value} ({failure.error_type}) is eligible for replanning",
        )


def _propagate(reason: str) -> RecoveryDecision:
    return RecoveryDecision(action=RecoveryAction.PROPAGATE, reason=reason)
