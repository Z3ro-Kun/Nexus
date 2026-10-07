from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from app.agents.planner import ClarificationRequest
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.events.types import (
    ArtifactRequirement,
    EventType,
    FactRequirement,
    Identifier,
    PolicyDecision,
    TaskCreated,
)
from app.models.run import RunStatus
from app.state.models import ApprovalState, TaskState, VerificationState


class RunCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=10_000)
    constraints: list[str] = Field(default_factory=list, max_length=20)


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    goal: str
    status: RunStatus
    last_sequence: int
    created_at: datetime
    updated_at: datetime


class EventIn(BaseModel):
    event_type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    agent_id: str | None = None
    task_id: str | None = None


class AppendEventsRequest(BaseModel):
    events: list[EventIn] = Field(min_length=1, max_length=100)
    expected_sequence: int | None = Field(default=None, ge=0)


class CreateTasksRequest(BaseModel):
    # Items use the TaskCreated event payload schema directly.
    tasks: list[TaskCreated] = Field(min_length=1, max_length=100)


class TaskRead(TaskState):
    run_id: UUID

    @classmethod
    def of(cls, run_id: UUID, task: TaskState) -> "TaskRead":
        return cls(run_id=run_id, **task.model_dump())


class ScheduleRequest(BaseModel):
    """`executor="agent"` runs tasks with LLM agents (needs a configured provider).
    `executor="scripted"` uses the scripted executor, which performs no work: each task
    reports the outcome given in `outcomes` (unlisted tasks succeed).

    `recovery` (agent executor only): replan eligible task failures, within
    NEXUS_MAX_REPLANS_PER_RUN (Phase 5). With it off, failures only propagate.

    `conflicts`: detect conflicting facts and create resolution tasks, within
    NEXUS_MAX_CONFLICT_RESOLUTIONS_PER_RUN (Phase 6). Deterministic; no LLM of its own.

    `verification`: run READY verification checkpoints (Phase 7). Deterministic checks
    always; the LLM semantic verifier only with the agent executor and a provider. With
    it off, checkpoints stay READY."""

    executor: Literal["agent", "scripted"] = "agent"
    outcomes: dict[str, Literal["success", "failure"]] = Field(default_factory=dict)
    recovery: bool = True
    conflicts: bool = True
    verification: bool = True
    # Phase 8: append RunCompleted when the pass ends and nothing blocks completion.
    complete: bool = True

    @model_validator(mode="after")
    def _outcomes_only_for_scripted(self) -> "ScheduleRequest":
        if self.outcomes and self.executor != "scripted":
            raise ValueError("outcomes can only be given with executor='scripted'")
        return self


class VerificationRequest(BaseModel):
    """Create a verification checkpoint (Phase 7). It runs when /schedule finds it READY:
    after every dependency completed (default: every current work task)."""

    task_id: Identifier = "verify_objective"
    title: str | None = Field(default=None, max_length=200)
    dependencies: list[Identifier] | None = Field(default=None, max_length=200)
    # Default: the run's goal.
    objective: str | None = Field(default=None, min_length=1, max_length=2000)
    required_facts: list[FactRequirement] = Field(default_factory=list, max_length=20)
    required_artifacts: list[ArtifactRequirement] = Field(default_factory=list, max_length=10)
    tool_evidence_tasks: list[Identifier] = Field(default_factory=list, max_length=20)
    semantic: bool = False


class RunVerification(BaseModel):
    """Every verification attempt of the run, and whether the run is verified: it has a
    checkpoint, and the current attempt of every checkpoint passed."""

    verified: bool
    verifications: list[VerificationState]


class ExecuteRequest(BaseModel):
    """Run the objective (Phase 9): plan once if needed, then schedule until the run
    completes, fails, waits for an approval or cannot progress. Call again to resume.
    `executor="scripted"` (testing) runs pre-created tasks without agents."""

    executor: Literal["agent", "scripted"] = "agent"
    outcomes: dict[str, Literal["success", "failure"]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _outcomes_only_for_scripted(self) -> "ExecuteRequest":
        if self.outcomes and self.executor != "scripted":
            raise ValueError("outcomes can only be given with executor='scripted'")
        return self


class ApprovalDecisionRequest(BaseModel):
    """A human decision on an approval. Recording it executes nothing; the next
    /schedule runs an approved action through the normal, gated path."""

    actor: str = Field(default="api_user", min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=1000)


class ApprovalContextItem(BaseModel):
    task_id: str
    title: str
    status: str
    summary: str | None
    facts: list[str]


class ApprovalView(BaseModel):
    """What a human needs to decide: the exact action, why it needs approval, and the
    results it builds on."""

    approval: ApprovalState
    task_title: str
    task_description: str | None
    task_status: str
    action_status: str
    decision: PolicyDecision
    context: list[ApprovalContextItem]


class PlanResponse(BaseModel):
    tasks: list[TaskRead]
    provider: str
    model: str
    # Phase 11: set (and `tasks` empty) when the planner asked for clarification instead;
    # the run is then needs_clarification and nothing will execute.
    clarification: ClarificationRequest | None = None
