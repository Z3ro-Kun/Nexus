"""The event contract: every event type and the schema of its payload.

Adding an event type means adding an `EventType` member, a payload class registered in
`PAYLOAD_TYPES`, and a projection handler. The persistence layer stores `event_type` as a
string and the payload as JSON, so it does not change.

Payloads describe *what happened*. The envelope's `agent_id` / `task_id` describe *who
emitted it and in which task context* (see `app.events.base.Event`).
"""

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictFloat,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)


class EventType(str, Enum):
    RUN_CREATED = "RunCreated"
    TASK_CREATED = "TaskCreated"
    TOOL_CALLED = "ToolCalled"
    TOOL_SUCCEEDED = "ToolSucceeded"
    TOOL_FAILED = "ToolFailed"
    FACT_ADDED = "FactAdded"
    ARTIFACT_ADDED = "ArtifactAdded"
    # Phase 10: workspace artifacts (generated files, projects, archives).
    ARTIFACT_CREATED = "ArtifactCreated"
    ARTIFACT_FILE_ADDED = "ArtifactFileAdded"
    ARTIFACT_VALIDATED = "ArtifactValidated"
    ARTIFACT_PACKAGED = "ArtifactPackaged"
    ARTIFACT_READY = "ArtifactReady"
    CONFLICT_DETECTED = "ConflictDetected"
    CONFLICT_RESOLVED = "ConflictResolved"
    CONFLICT_UNRESOLVED = "ConflictUnresolved"
    TASK_STARTED = "TaskStarted"
    TASK_COMPLETED = "TaskCompleted"
    TASK_FAILED = "TaskFailed"
    TASK_CANCELLED = "TaskCancelled"
    REPLAN_TRIGGERED = "ReplanTriggered"
    REPLAN_REJECTED = "ReplanRejected"
    VERIFICATION_STARTED = "VerificationStarted"
    VERIFICATION_PASSED = "VerificationPassed"
    VERIFICATION_FAILED = "VerificationFailed"
    APPROVAL_REQUESTED = "ApprovalRequested"
    APPROVAL_GRANTED = "ApprovalGranted"
    APPROVAL_REJECTED = "ApprovalRejected"
    POLICY_EVALUATED = "PolicyEvaluated"
    RUN_COMPLETED = "RunCompleted"
    RUN_FAILED = "RunFailed"
    # Phase 11: the planner asked for clarification instead of planning.
    CLARIFICATION_REQUESTED = "ClarificationRequested"


Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class FailureType(str, Enum):
    """Why a task failed, in the coarse terms recovery decides on (Phase 5).

    Set deterministically by `app.recovery.classifier`, never by an LLM."""

    TOOL_FAILURE = "TOOL_FAILURE"  # a tool call failed (unavailable, HTTP error, bad output...)
    AGENT_FAILURE = "AGENT_FAILURE"  # the agent/LLM failed or reported it could not do the task
    PLANNING_FAILURE = "PLANNING_FAILURE"  # the task as planned cannot run (agent/task type)
    VALIDATION_FAILURE = "VALIDATION_FAILURE"  # the agent's output failed NEXUS validation
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"  # blocked by a failed dependency (never replanned itself)
    TIMEOUT = "TIMEOUT"  # the agent or a tool exceeded its time limit
    POLICY_FAILURE = "POLICY_FAILURE"  # a policy refused an action (unauthorized tool, SSRF...)
    # Phase 7: an independent verification found that completed work does not meet its
    # requirements (only ever the failure of a verification task).
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    # An LLM provider refused service (rate limit / quota, HTTP 429): not a problem with the
    # task, so a different plan cannot fix it; never replanned.
    PROVIDER_FAILURE = "PROVIDER_FAILURE"


class EventPayload(BaseModel):
    """Base class for payloads. Payloads are immutable and must be JSON-serializable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: ClassVar[EventType]


class RunCreated(EventPayload):
    event_type = EventType.RUN_CREATED
    goal: str = Field(min_length=1)
    # Added in Phase 3 (default keeps earlier events valid). Passed to planner and agents.
    constraints: list[Annotated[str, StringConstraints(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=20
    )


class Evidence(BaseModel):
    """What a result is based on: the task context the agent was given, the model's own
    (unverified) knowledge, or the output of a tool call made for the task (Phase 4;
    `reference` is then the tool_call_id, checked by the runtime)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["task_context", "model_knowledge", "tool_output"]
    reference: str | None = Field(default=None, max_length=200)
    note: str = Field(min_length=1, max_length=1000)


ProvenanceKind = Literal["model_knowledge", "tool_output", "task_context", "user_provided"]


class Provenance(BaseModel):
    """Where a fact came from. Set by the agent runtime, never taken from the LLM as-is:
    for `tool_output` the tool name, call id, source and time come from the runtime's own
    record of a successful tool call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ProvenanceKind
    tool_name: str | None = Field(default=None, max_length=64)
    tool_call_id: str | None = Field(default=None, max_length=128)
    # For tool output: the URL or a description of the input (e.g. the expression).
    source: str | None = Field(default=None, max_length=2000)
    retrieved_at: datetime | None = None
    # True when the tool was a deterministic fake: the content is not real-world data.
    fake: bool = False


class FactClaim(BaseModel):
    """A fact's structured claim (Phase 6): `subject` / `attribute` = `value` [`unit`].

    The (normalized) subject and attribute form the fact's logical key, used for
    deterministic conflict detection; the free-text `content` is never the identity.
    A number compares numerically, a string as normalized text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    attribute: str = Field(min_length=1, max_length=100)
    value: StrictInt | StrictFloat | Annotated[str, StringConstraints(min_length=1, max_length=500)]
    unit: str | None = Field(default=None, min_length=1, max_length=32)


class FactKey(BaseModel):
    """The normalized logical identity of a claim: lowercase, whitespace collapsed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    attribute: str = Field(min_length=1, max_length=100)


class ConflictType(str, Enum):
    NUMERIC_DISAGREEMENT = "NUMERIC_DISAGREEMENT"  # numbers in the same unit differ
    TEXTUAL_DISAGREEMENT = "TEXTUAL_DISAGREEMENT"  # texts in the same unit differ
    ATTRIBUTE_DISAGREEMENT = "ATTRIBUTE_DISAGREEMENT"  # not comparable: units differ, or number vs text


# --- Verification (Phase 7) ---------------------------------------------------------------

# A verification checkpoint is a task with this agent and task type, and a VerificationSpec.
VERIFIER_AGENT_TYPE = "verifier"
VERIFICATION_TASK_TYPE = "verification"

ClaimValue = StrictInt | StrictFloat | Annotated[str, StringConstraints(min_length=1, max_length=500)]


class ValueConstraint(BaseModel):
    """A deterministic constraint on a fact's value, e.g. price <= 100000 INR. Numbers
    support every operator; text (compared normalized) only eq / ne."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operator: Literal["eq", "ne", "lt", "le", "gt", "ge"]
    value: ClaimValue
    unit: str | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def _text_only_equality(self) -> "ValueConstraint":
        if isinstance(self.value, str) and self.operator not in ("eq", "ne"):
            raise ValueError("a text value only supports eq and ne")
        return self


class FactRequirement(BaseModel):
    """The verified work must establish a usable value for subject / attribute (Phase 6
    `current_value`: accepted or corroborated, never conflicting)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    attribute: str = Field(min_length=1, max_length=100)
    # The value must be backed by at least one tool-derived fact (provenance tool_output).
    tool_derived: bool = False
    constraint: ValueConstraint | None = None


class ArtifactRequirement(BaseModel):
    """The verified work must include an artifact with this name (case-insensitive). With
    `json_fields`, its content must be a JSON object with each field present and not null."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    media_type: Literal["text/plain", "text/markdown", "application/json"] | None = None
    json_fields: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=20
    )

    @model_validator(mode="after")
    def _json_fields_need_json(self) -> "ArtifactRequirement":
        if self.json_fields and self.media_type not in (None, "application/json"):
            raise ValueError("json_fields require media_type application/json")
        return self


class VerificationSpec(BaseModel):
    """What a verification checkpoint requires. Fixed when the checkpoint is created; a
    replacement checkpoint (after recovery) must carry exactly the same spec."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    objective: str = Field(min_length=1, max_length=2000)
    required_facts: list[FactRequirement] = Field(default_factory=list, max_length=20)
    required_artifacts: list[ArtifactRequirement] = Field(default_factory=list, max_length=10)
    # Covered tasks that must have produced at least one valid tool-derived fact.
    tool_evidence_tasks: list[Identifier] = Field(default_factory=list, max_length=20)
    # Also ask the LLM verifier whether the objective and the run's constraints are met.
    semantic: bool = False

    @field_validator("tool_evidence_tasks")
    @classmethod
    def _unique_tasks(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("tool_evidence_tasks must not contain duplicates")
        return value


class EvidenceRef(BaseModel):
    """A reference to something in the run's state. Built by NEXUS; model output is only
    turned into one after checking that the referenced item exists."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["task", "fact", "artifact", "tool_call", "conflict"]
    id: Identifier


class CheckKind(str, Enum):
    TASKS_COMPLETED = "tasks_completed"
    PROVENANCE = "provenance"
    CONFLICTS = "conflicts"
    REQUIRED_FACT = "required_fact"
    REQUIRED_ARTIFACT = "required_artifact"
    TOOL_EVIDENCE = "tool_evidence"
    ACTIONS = "actions"  # Phase 8: covered action tasks were authorized and executed as approved
    ARTIFACTS = "artifacts"  # Phase 10: covered work's generated files were validated (and packaged)


class VerificationCheck(BaseModel):
    """The result of one deterministic check (app.verification.checks)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str = Field(min_length=1, max_length=300)
    kind: CheckKind
    passed: bool
    message: str = Field(min_length=1, max_length=2000)
    references: list[EvidenceRef] = Field(default_factory=list, max_length=500)


class SemanticJudgement(BaseModel):
    """The LLM verifier's judgement of one criterion. `criterion` is set by NEXUS (the
    objective or a constraint's text); `evidence` holds only references that exist."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion: str = Field(min_length=1, max_length=2000)
    passed: bool
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=20)
    explanation: str = Field(min_length=1, max_length=1000)


class SemanticVerification(BaseModel):
    """The LLM verifier's structured verdict: untrusted model output, validated and
    stored as data. `passed` is derived by NEXUS from the judgements."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=200)
    passed: bool
    objective: SemanticJudgement
    constraints: list[SemanticJudgement] = Field(default_factory=list, max_length=20)
    summary: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _passed_is_derived(self) -> "SemanticVerification":
        if self.passed != (self.objective.passed and all(c.passed for c in self.constraints)):
            raise ValueError("passed must hold exactly when every judgement passed")
        return self


# --- Policy and approval (Phase 8) ----------------------------------------------------------

# An action task performs exactly one predeclared tool call (its ActionSpec), and only
# after the policy gate (and, where required, a human approval) allowed it.
ACTION_AGENT_TYPE = "action_executor"
ACTION_TASK_TYPE = "action"


class ActionCategory(str, Enum):
    """What executing an action can do to the world. Authoritative values come from
    registered tool metadata (ToolDefinition.category), never from model output."""

    READ_ONLY = "read_only"  # pure computation or reading run data
    NETWORK_READ = "network_read"  # reads from the network; no side effects
    REVERSIBLE_WRITE = "reversible_write"  # changes something that can be undone
    IRREVERSIBLE = "irreversible"  # external side effect that cannot be undone


class PolicyOutcome(str, Enum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    DENY = "deny"


class ActionSpec(BaseModel):
    """The exact action an action task performs: one tool call, fixed at creation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    # What the action is meant to achieve, for the human approver.
    intent: str = Field(min_length=1, max_length=1000)


def action_fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
    """Identity of a concrete action: the tool plus its canonical JSON arguments."""
    material = json.dumps({"tool": tool_name, "arguments": arguments}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()[:32]


class PolicyDecision(BaseModel):
    """A deterministic policy decision (app.policy.engine). Structured: the outcome,
    the rule that produced it and the risk category are values, not prose."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: Identifier
    tool_name: str = Field(min_length=1, max_length=64)
    action_fingerprint: str = Field(min_length=1, max_length=64)
    # None when the tool is not registered (then the outcome is DENY).
    category: ActionCategory | None
    outcome: PolicyOutcome
    # e.g. "category:irreversible", "denied_tool", "unknown_tool", "previously_rejected".
    rule: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)

    @property
    def approval_required(self) -> bool:
        return self.outcome is PolicyOutcome.APPROVAL_REQUIRED


class TaskCreated(EventPayload):
    """A task was added to the run's task graph.

    Fields after `description` were added in Phase 2 with defaults, so TaskCreated events
    written before them remain valid.
    """

    event_type = EventType.TASK_CREATED
    task_id: Identifier
    title: str = Field(min_length=1)
    description: str | None = None
    task_type: Identifier | None = None
    agent_type: Identifier | None = None
    parent_id: Identifier | None = None
    dependencies: list[Identifier] = Field(default_factory=list)
    # Phase 5: this task replaces the named FAILED task (created by recovery). The failed
    # task stays FAILED; its dependents are satisfied through the replacement instead.
    replaces: Identifier | None = None
    # Phase 6: this task resolves the named OPEN conflict (created by conflict handling).
    conflict_id: Identifier | None = None
    # Phase 7: this task is a verification checkpoint over its dependencies.
    verification: VerificationSpec | None = None
    # Phase 8: this task performs exactly this action, after the policy gate.
    action: ActionSpec | None = None

    @field_validator("dependencies")
    @classmethod
    def _unique_dependencies(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("dependencies must not contain duplicates")
        return value


class TaskStarted(EventPayload):
    """The scheduler claimed the task and handed it to an executor."""

    event_type = EventType.TASK_STARTED
    task_id: Identifier


class ToolCalled(EventPayload):
    """An agent requested a tool call. Recorded whether or not the call passed validation;
    ToolSucceeded or ToolFailed always follows."""

    event_type = EventType.TOOL_CALLED
    tool_call_id: Identifier
    tool_name: str = Field(min_length=1)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ToolSucceeded(EventPayload):
    event_type = EventType.TOOL_SUCCEEDED
    tool_call_id: Identifier
    result: JsonValue = None
    # Added in Phase 4 (default keeps earlier events valid): duration, fake flag, etc.
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ToolFailed(EventPayload):
    event_type = EventType.TOOL_FAILED
    tool_call_id: Identifier
    error: str = Field(min_length=1)
    # Added in Phase 4: e.g. "unauthorized", "invalid_arguments", "timeout", "ssrf_blocked".
    error_type: str | None = Field(default=None, max_length=64)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class FactAdded(EventPayload):
    event_type = EventType.FACT_ADDED
    fact_id: Identifier
    content: str = Field(min_length=1)
    source: str | None = None
    # Added in Phase 4 (None for earlier events, whose origin was not recorded).
    provenance: Provenance | None = None
    # Added in Phase 6: the structured claim, if the fact states a comparable value.
    claim: FactClaim | None = None


class ArtifactAdded(EventPayload):
    """A text artifact produced for the run (e.g. a comparison table in Markdown)."""

    event_type = EventType.ARTIFACT_ADDED
    artifact_id: Identifier
    name: str = Field(min_length=1, max_length=200)
    media_type: Literal["text/plain", "text/markdown", "application/json"]
    content: str = Field(min_length=1, max_length=50_000)


# --- Workspace artifacts (Phase 10) ----------------------------------------------------------
#
# Files an agent generated with the artifact_write tool, kept in the controlled artifact
# workspace (app.artifacts.workspace); the log records what each deliverable contains
# and the SHA-256 of every file and archive, never the bytes. Written by the agent runtime
# with the creating task's TaskCompleted (created, files, validated, packaged), and by
# RunCompletion (ready, or a failed re-validation) after verification. A deliverable is
# downloadable only once ArtifactReady is recorded.


class ArtifactCreated(EventPayload):
    """A file or project artifact of the task in the envelope. `tool_call_ids`: its
    artifact_write calls; `input_fact_ids`: the facts the task was given (its
    dependencies' results), i.e. what it could be derived from. `supersedes`: the current
    artifact of the same type and name that this one replaces, made by a direct dependency
    of this recovery task (a fixed version); it is marked superseded once this one passes
    validation."""

    event_type = EventType.ARTIFACT_CREATED
    artifact_id: Identifier
    name: str = Field(min_length=1, max_length=64)
    artifact_type: Literal["file", "project"]
    tool_call_ids: list[Identifier] = Field(min_length=1, max_length=100)
    input_fact_ids: list[Identifier] = Field(default_factory=list, max_length=1000)
    supersedes: Identifier | None = None


class ArtifactFileAdded(EventPayload):
    """One file of an artifact: its relative path, and size and SHA-256 of its bytes."""

    event_type = EventType.ARTIFACT_FILE_ADDED
    artifact_id: Identifier
    path: str = Field(min_length=1, max_length=300)
    media_type: str = Field(min_length=1, max_length=100)
    size: int = Field(ge=0)
    sha256: Sha256Hex
    tool_call_id: Identifier


class ArtifactValidated(EventPayload):
    """Deterministic validation of an artifact's stored files (paths, exclusions, limits,
    secrets, checksums). Failed: `problems` say why (never file content)."""

    event_type = EventType.ARTIFACT_VALIDATED
    artifact_id: Identifier
    passed: bool
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    problems: list[Annotated[str, StringConstraints(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=20)


class ArtifactPackaged(EventPayload):
    """A validated project packaged as a ZIP archive: a new archive artifact
    (`artifact_id`) holding exactly the project's files under `<project>/`."""

    event_type = EventType.ARTIFACT_PACKAGED
    artifact_id: Identifier
    source_artifact_id: Identifier
    name: str = Field(min_length=1, max_length=80)
    media_type: Literal["application/zip"] = "application/zip"
    size: int = Field(gt=0)
    sha256: Sha256Hex
    paths: list[str] = Field(min_length=1, max_length=10_000)


class ArtifactReady(EventPayload):
    """A verified deliverable (a file or an archive) whose stored bytes were re-checked
    against `sha256` just before the run completed. Only these can be downloaded."""

    event_type = EventType.ARTIFACT_READY
    artifact_id: Identifier
    sha256: Sha256Hex


class ConflictDetected(EventPayload):
    """Facts disagree. Phase 6 (structured) form: `conflict_type`, `fact_key`,
    `fingerprint` and `reason` are set, and `fact_ids` are >= 2 facts with claims on
    `fact_key` whose values disagree (checked by the projector). The pre-Phase 6 form
    (`description` only) is still accepted. `detected_at` is the envelope timestamp."""

    event_type = EventType.CONFLICT_DETECTED
    conflict_id: Identifier
    description: str | None = Field(default=None, min_length=1)
    fact_ids: list[Identifier] = Field(default_factory=list)
    conflict_type: ConflictType | None = None
    fact_key: FactKey | None = None
    # sha256 over the conflict type and the sorted fact ids; unique per run.
    fingerprint: str | None = Field(default=None, max_length=64)
    reason: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _legacy_or_structured(self) -> "ConflictDetected":
        structured = (self.conflict_type, self.fact_key, self.fingerprint, self.reason)
        if all(v is None for v in structured):
            if self.description is None:
                raise ValueError("description is required for a ConflictDetected without conflict_type")
        elif any(v is None for v in structured):
            raise ValueError("conflict_type, fact_key, fingerprint and reason must be given together")
        return self


class ConflictResolved(EventPayload):
    """A conflict was resolved. Phase 6 (structured) form: `resolved_fact_id` is the
    currently accepted fact, backed by `evidence_fact_ids` produced by
    `resolver_task_id` (tool-derived, same fact key; checked by the projector). The
    original conflicting facts are never removed. The pre-Phase 6 form (`resolution`
    text only) is accepted only for conflicts detected in the pre-Phase 6 form."""

    event_type = EventType.CONFLICT_RESOLVED
    conflict_id: Identifier
    resolution: str | None = Field(default=None, min_length=1)
    resolved_fact_id: Identifier | None = None
    evidence_fact_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    # Original conflicting facts whose value matches the accepted one.
    corroborated_fact_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    resolver_task_id: Identifier | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _legacy_or_structured(self) -> "ConflictResolved":
        structured = (self.resolved_fact_id, self.resolver_task_id, self.reason)
        if all(v is None for v in structured) and not self.evidence_fact_ids:
            if self.resolution is None:
                raise ValueError("resolution is required for a ConflictResolved without resolved_fact_id")
        elif any(v is None for v in structured) or not self.evidence_fact_ids:
            raise ValueError(
                "resolved_fact_id, evidence_fact_ids, resolver_task_id and reason must be given together"
            )
        return self


class ConflictUnresolved(EventPayload):
    """The conflict's resolution task finished without a reliable result (no
    independent tool-derived evidence, or the evidence disagrees). No winner is chosen;
    all facts and evidence stay in history."""

    event_type = EventType.CONFLICT_UNRESOLVED
    conflict_id: Identifier
    resolver_task_id: Identifier
    evidence_fact_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=1000)


class TaskCompleted(EventPayload):
    event_type = EventType.TASK_COMPLETED
    task_id: Identifier
    summary: str | None = None
    # Added in Phase 3 with defaults, so earlier TaskCompleted events remain valid.
    evidence: list[Evidence] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class TaskFailed(EventPayload):
    event_type = EventType.TASK_FAILED
    task_id: Identifier
    # A safe message: truncated, with credentials redacted (app.core.redaction).
    error: str = Field(min_length=1)
    # Added in Phase 5 (None for earlier events): the deterministic classification.
    failure_type: FailureType | None = None
    # Finer-grained cause, e.g. "agent_timeout", "unavailable", "invalid_provenance".
    error_type: str | None = Field(default=None, max_length=64)
    # The tool call that caused the failure, if any.
    tool_call_id: Identifier | None = None


class TaskCancelled(EventPayload):
    """A task that has not started is withdrawn. Running tasks cannot be cancelled."""

    event_type = EventType.TASK_CANCELLED
    task_id: Identifier
    reason: str | None = None


class ReplanTriggered(EventPayload):
    """Recovery accepted a replan for a failed task. The new tasks follow as TaskCreated
    events in the same append. Concise structured metadata only; no model reasoning.

    Fields after `reason` were added in Phase 5. A ReplanTriggered without
    `failed_task_id` (possible before Phase 5) is accepted and changes no state."""

    event_type = EventType.REPLAN_TRIGGERED
    reason: str = Field(min_length=1, max_length=1000)
    failed_task_id: Identifier | None = None
    failure_type: FailureType | None = None
    # 1-based count of replanner invocations in this run (accepted + rejected).
    replan_number: int | None = Field(default=None, ge=1)
    strategy_summary: str | None = Field(default=None, max_length=1000)
    new_task_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    replacement_task_id: Identifier | None = None
    plan_fingerprint: str | None = Field(default=None, max_length=64)


class ReplanRejected(EventPayload):
    """A replanner invocation produced nothing usable (provider error, timeout, or a plan
    that failed validation). Nothing was added to the task graph. Counts against the
    run's replan budget."""

    event_type = EventType.REPLAN_REJECTED
    failed_task_id: Identifier
    failure_type: FailureType
    replan_number: int = Field(ge=1)
    # "provider", "timeout", "schema", "policy", "graph" or "duplicate".
    stage: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=1000)
    plan_fingerprint: str | None = Field(default=None, max_length=64)


def _check_verification_form(payload: Any, has_phase7_fields: bool) -> None:
    """Phase 7 form: `task_id` names the verification task and equals `verification_id`.
    Without `task_id` (the pre-Phase 7 form) the event is recorded only, and the Phase 7
    fields must be empty."""
    if payload.task_id is None:
        if has_phase7_fields:
            raise ValueError("Phase 7 verification fields require task_id")
    elif payload.verification_id != payload.task_id:
        raise ValueError("verification_id must equal task_id")


class VerificationStarted(EventPayload):
    """A verification checkpoint started. Phase 7 form: records exactly what is being
    verified, computed from state by NEXUS and re-checked by the projector."""

    event_type = EventType.VERIFICATION_STARTED
    verification_id: Identifier
    task_id: Identifier | None = None
    # The run's last sequence when the verification context was built.
    based_on_sequence: int | None = Field(default=None, ge=1)
    covered_task_ids: list[Identifier] = Field(default_factory=list, max_length=500)
    fact_ids: list[Identifier] = Field(default_factory=list, max_length=5000)
    artifact_ids: list[Identifier] = Field(default_factory=list, max_length=1000)
    tool_call_ids: list[Identifier] = Field(default_factory=list, max_length=5000)
    conflict_ids: list[Identifier] = Field(default_factory=list, max_length=500)
    semantic: bool = False

    @model_validator(mode="after")
    def _form(self) -> "VerificationStarted":
        _check_verification_form(
            self,
            bool(
                self.based_on_sequence or self.covered_task_ids or self.fact_ids or self.artifact_ids
                or self.tool_call_ids or self.conflict_ids or self.semantic
            ),
        )
        if self.task_id is not None and (self.based_on_sequence is None or not self.covered_task_ids):
            raise ValueError("based_on_sequence and covered_task_ids are required with task_id")
        return self


class VerificationPassed(EventPayload):
    """Phase 7 form: every deterministic check passed, and the semantic verdict too if
    the checkpoint requires one. The projector re-runs the deterministic checks."""

    event_type = EventType.VERIFICATION_PASSED
    verification_id: Identifier
    details: str | None = Field(default=None, max_length=2000)
    task_id: Identifier | None = None
    checks: list[VerificationCheck] = Field(default_factory=list, max_length=100)
    semantic: SemanticVerification | None = None

    @model_validator(mode="after")
    def _form(self) -> "VerificationPassed":
        _check_verification_form(self, bool(self.checks or self.semantic))
        if self.task_id is not None and not self.checks:
            raise ValueError("checks are required with task_id")
        return self


class VerificationFailed(EventPayload):
    """Phase 7 form: a deterministic check failed, or the semantic verdict failed. The
    verification task's TaskFailed (VERIFICATION_FAILURE) feeds Phase 5 recovery."""

    event_type = EventType.VERIFICATION_FAILED
    verification_id: Identifier
    reason: str = Field(min_length=1, max_length=2000)
    task_id: Identifier | None = None
    checks: list[VerificationCheck] = Field(default_factory=list, max_length=100)
    semantic: SemanticVerification | None = None

    @model_validator(mode="after")
    def _form(self) -> "VerificationFailed":
        _check_verification_form(self, bool(self.checks or self.semantic))
        if self.task_id is not None and not self.checks:
            raise ValueError("checks are required with task_id")
        return self


class PolicyEvaluated(EventPayload):
    """The policy gate decided on an action task, before it could start (Phase 8).
    Written only by the PolicyManager (privileged; see app.events.authority)."""

    event_type = EventType.POLICY_EVALUATED
    decision: PolicyDecision


class ApprovalRequested(EventPayload):
    """Phase 8 form (with `task_id`): the action task's policy decision requires a human
    approval; the action waits until it is granted or rejected. Without `task_id` (the
    pre-Phase 8 form) the event is recorded only."""

    event_type = EventType.APPROVAL_REQUESTED
    approval_id: Identifier
    description: str = Field(min_length=1, max_length=2000)
    task_id: Identifier | None = None
    action: ActionSpec | None = None

    @model_validator(mode="after")
    def _form(self) -> "ApprovalRequested":
        if (self.task_id is None) != (self.action is None):
            raise ValueError("task_id and action must be given together")
        return self


class ApprovalGranted(EventPayload):
    """Phase 8 form (with `task_id`): a human granted the approval. Granting only
    allows the action; the scheduler then executes it through the normal path."""

    event_type = EventType.APPROVAL_GRANTED
    approval_id: Identifier
    task_id: Identifier | None = None
    actor: str | None = Field(default=None, min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=1000)


class ApprovalRejected(EventPayload):
    """Phase 8 form (with `task_id`): a human rejected the approval; the action never
    executes and its task fails (POLICY_FAILURE, approval_rejected)."""

    event_type = EventType.APPROVAL_REJECTED
    approval_id: Identifier
    reason: str | None = Field(default=None, max_length=1000)
    task_id: Identifier | None = None
    actor: str | None = Field(default=None, min_length=1, max_length=200)


class RunCompleted(EventPayload):
    event_type = EventType.RUN_COMPLETED
    summary: str | None = None


class RunFailed(EventPayload):
    event_type = EventType.RUN_FAILED
    reason: str = Field(min_length=1)


class ClarificationRequested(EventPayload):
    """The planner decided the objective cannot be executed as stated (Phase 11). Recorded
    by PlanningService instead of a plan, only for a run with no tasks; it ends the run in
    status needs_clarification: nothing is planned, executed, recovered or verified, and
    the goal is unchanged. `reason`: underspecified (a statement or wish that does not say
    what work is wanted) or not_a_request (conversational input)."""

    event_type = EventType.CLARIFICATION_REQUESTED
    reason: Literal["underspecified", "not_a_request"]
    question: str = Field(min_length=5, max_length=300)
    missing: list[Annotated[str, StringConstraints(min_length=1, max_length=200)]] = Field(min_length=1, max_length=5)
    # Which planner model decided (provenance).
    provider: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=200)


PAYLOAD_TYPES: dict[EventType, type[EventPayload]] = {
    cls.event_type: cls
    for cls in (
        RunCreated,
        TaskCreated,
        ToolCalled,
        ToolSucceeded,
        ToolFailed,
        FactAdded,
        ArtifactAdded,
        ArtifactCreated,
        ArtifactFileAdded,
        ArtifactValidated,
        ArtifactPackaged,
        ArtifactReady,
        ConflictDetected,
        ConflictResolved,
        ConflictUnresolved,
        TaskStarted,
        TaskCompleted,
        TaskFailed,
        TaskCancelled,
        ReplanTriggered,
        ReplanRejected,
        VerificationStarted,
        VerificationPassed,
        VerificationFailed,
        ApprovalRequested,
        ApprovalGranted,
        ApprovalRejected,
        PolicyEvaluated,
        RunCompleted,
        RunFailed,
        ClarificationRequested,
    )
}

_missing = set(EventType) - PAYLOAD_TYPES.keys()
if _missing:  # pragma: no cover - guards the contract at import time
    raise RuntimeError(f"event types without payload schema: {sorted(_missing)}")


def payload_to_json(payload: EventPayload) -> dict[str, Any]:
    return payload.model_dump(mode="json")
