"""Application error types and their translation to HTTP responses."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class NexusError(Exception):
    """Base class for expected, domain-level errors surfaced to API clients."""

    status_code: int = 400
    code: str = "nexus_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RunNotFoundError(NexusError):
    status_code = 404
    code = "run_not_found"


class InvalidEventError(NexusError):
    """An event is malformed or violates the projection rules for the run's history."""

    status_code = 422
    code = "invalid_event"


class UnsupportedEventError(NexusError):
    """An event type has no registered payload schema or projection handler."""

    status_code = 422
    code = "unsupported_event"


class SequenceConflictError(NexusError):
    """The run's event log advanced past the sequence the writer expected."""

    status_code = 409
    code = "sequence_conflict"


class AppendOnlyViolationError(NexusError):
    """Something attempted to update or delete a stored event."""

    status_code = 500
    code = "append_only_violation"


class TaskNotFoundError(NexusError):
    status_code = 404
    code = "task_not_found"


class RunNotActiveError(NexusError):
    """The run is completed or failed, so no tasks can be scheduled."""

    status_code = 409
    code = "run_not_active"


class SchedulerContentionError(NexusError):
    """The scheduler could not append an event after repeated sequence conflicts."""

    status_code = 503
    code = "scheduler_contention"


# --- LLM and planning -------------------------------------------------------------------


class LLMError(NexusError):
    """A provider call failed. NEXUS adds no retries beyond the provider client's own."""

    status_code = 502
    code = "llm_error"


class LLMTimeoutError(LLMError):
    status_code = 504
    code = "llm_timeout"


class LLMRateLimitError(LLMError):
    """The provider refused the request because of a rate limit or quota (HTTP 429). A
    provider availability problem, not a problem with the task: recovery does not replan
    it (see app.recovery.policy)."""

    status_code = 503
    code = "llm_rate_limited"


class LLMResponseError(LLMError):
    """The provider answered, but not with a usable structured response."""

    code = "llm_invalid_response"


class LLMContentFilteredError(LLMResponseError):
    """The model declined the request, or the provider blocked its output with a content
    filter (e.g. Gemini's "content_filter: RECITATION"). Not an empty response:
    `provider_reason` keeps what the provider said (finish/stop reason or refusal)."""

    code = "llm_content_filtered"

    def __init__(self, message: str, *, provider_reason: str | None = None) -> None:
        super().__init__(message)
        self.provider_reason = provider_reason


class LLMConfigurationError(LLMError):
    """The LLM routing configuration cannot serve a request (e.g. the role's provider has
    no API key). Raised before anything is sent. Messages name settings, never values."""

    status_code = 500
    code = "llm_misconfigured"


class LLMNotConfiguredError(NexusError):
    status_code = 503
    code = "llm_not_configured"


class VerifierUnavailableError(NexusError):
    """A checkpoint needs the LLM verifier, but none is configured (Phase 7)."""

    status_code = 503
    code = "verifier_unavailable"


class ApprovalNotFoundError(NexusError):
    status_code = 404
    code = "approval_not_found"


class ApprovalNotPendingError(NexusError):
    """The approval was already decided, or its task is no longer waiting (Phase 8)."""

    status_code = 409
    code = "approval_not_pending"


class PrivilegedEventError(NexusError):
    """An external writer tried to append an event only NEXUS managers may write (Phase 8)."""

    status_code = 403
    code = "privileged_event"


class PlanRejectedError(NexusError):
    """Planner output failed schema, policy or graph validation. Nothing was written."""

    status_code = 422
    code = "plan_rejected"

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage


class PlanAlreadyExistsError(NexusError):
    status_code = 409
    code = "plan_exists"


class UnknownAgentTypeError(NexusError):
    status_code = 422
    code = "unknown_agent_type"


# --- Task graph validation ---------------------------------------------------------------


class TaskGraphError(NexusError):
    """Base class for invalid task graphs. Invalid graphs are rejected, never repaired."""

    status_code = 422
    code = "invalid_task_graph"


class DuplicateTaskError(TaskGraphError):
    code = "duplicate_task"


class SelfDependencyError(TaskGraphError):
    code = "self_dependency"


class MissingDependencyError(TaskGraphError):
    code = "missing_dependency"


class UnknownParentError(TaskGraphError):
    code = "unknown_parent"


class DependencyCycleError(TaskGraphError):
    code = "dependency_cycle"


class InvalidReplacementError(TaskGraphError):
    """A task's `replaces` does not name an existing, FAILED, not-yet-replaced task."""

    code = "invalid_replacement"


class NonViableDependencyError(TaskGraphError):
    """A new task depends on a task that is failed, cancelled or blocked."""

    code = "non_viable_dependency"


async def _handle_nexus_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, NexusError)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


# --- Artifacts (Phase 10) ----------------------------------------------------------------


class ArtifactNotFoundError(NexusError):
    status_code = 404
    code = "artifact_not_found"


class ArtifactNotDeliverableError(NexusError):
    """The artifact exists but is not a downloadable deliverable (yet): not verified and
    ready, or a project (download its archive)."""

    status_code = 409
    code = "artifact_not_ready"


class ArtifactMissingError(NexusError):
    """A ready artifact's stored bytes are gone from the artifact workspace."""

    status_code = 410
    code = "artifact_missing"


class ArtifactCorruptedError(NexusError):
    """A ready artifact's stored bytes no longer match its recorded checksum."""

    status_code = 500
    code = "artifact_corrupted"


class ArtifactsDisabledError(NexusError):
    status_code = 503
    code = "artifacts_disabled"


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(NexusError, _handle_nexus_error)
