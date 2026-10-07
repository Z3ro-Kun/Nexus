"""Event authority: which event types external writers may append (Phase 8).

The raw events API (`POST /runs/{id}/events`) is an external writer. It may append
ordinary run history, which the projector validates as always, but not the event types
that carry authority. Those are written only by their NEXUS managers, in-process:

    PolicyEvaluated                         PolicyManager (the policy gate)
    ApprovalRequested                       PolicyManager
    ApprovalGranted / ApprovalRejected      ApprovalManager (the human approval endpoints)
    VerificationStarted / Passed / Failed   VerificationManager
    RunCompleted                            RunCompletion (after the completion gate)
    Artifact{Created,FileAdded,Validated,   the agent runtime (from the artifact workspace)
      Packaged}
    ArtifactReady                           RunCompletion (after re-checking the bytes)
    ClarificationRequested                  PlanningService (the planner's decision)

So a caller cannot forge an approval, a policy decision, a verification verdict or a
completion by writing the event itself; worker/agent results cannot contain them either
(the scheduler records only facts, artifacts and tool events from executors). This is a
deterministic rule, not authentication: the human approval endpoints are the channel
for approvals.
"""

from collections.abc import Iterable

from app.core.exceptions import PrivilegedEventError
from app.events.types import EventType

PRIVILEGED_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.POLICY_EVALUATED,
        EventType.APPROVAL_REQUESTED,
        EventType.APPROVAL_GRANTED,
        EventType.APPROVAL_REJECTED,
        EventType.VERIFICATION_STARTED,
        EventType.VERIFICATION_PASSED,
        EventType.VERIFICATION_FAILED,
        EventType.RUN_COMPLETED,
        EventType.ARTIFACT_CREATED,
        EventType.ARTIFACT_FILE_ADDED,
        EventType.ARTIFACT_VALIDATED,
        EventType.ARTIFACT_PACKAGED,
        EventType.ARTIFACT_READY,
        EventType.CLARIFICATION_REQUESTED,
    }
)


def check_external_append(event_types: Iterable[EventType]) -> None:
    """Raise PrivilegedEventError if an external writer tries to append a privileged type."""
    privileged = sorted({t.value for t in event_types if t in PRIVILEGED_EVENT_TYPES})
    if privileged:
        raise PrivilegedEventError(
            f"event types {privileged} can only be written by NEXUS itself (policy gate, approval "
            "endpoints, verification, run completion), not through the events API"
        )
