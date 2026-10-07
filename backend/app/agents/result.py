"""Structured agent output.

`AgentReport` is the schema the LLM must fill (sent as the structured-output schema and
validated afterwards). `AgentStep` wraps it for agents that may use tools: each LLM turn
either requests one tool call or finishes with a report. `AgentResult` adds runtime
metadata. None of these is authoritative state: the runtime turns an accepted result into
events, and state is projected from them.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.events.types import Evidence, FactClaim, ProvenanceKind
from app.tools.schemas import ToolCall


class AgentFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str = Field(min_length=1, max_length=2000)
    # What the fact rests on. Defaults to the weakest claim. "tool_output" is accepted only
    # if tool_call_id names a successful tool call of this task (checked by the runtime).
    basis: ProvenanceKind = "model_knowledge"
    tool_call_id: str | None = Field(default=None, max_length=128)
    # Optional: must be a URL that appears in the cited tool call's output.
    source_url: str | None = Field(default=None, max_length=2000)
    # Optional (Phase 6): subject / attribute = value [unit], for comparable facts. Used
    # by deterministic conflict detection; the runtime records it unchanged.
    claim: FactClaim | None = None


class AgentArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    media_type: Literal["text/plain", "text/markdown", "application/json"]
    content: str = Field(min_length=1, max_length=50_000)


class AgentReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    summary: str = Field(min_length=1, max_length=4000)
    facts: list[AgentFact] = Field(max_length=20)
    evidence: list[Evidence] = Field(max_length=20)
    artifacts: list[AgentArtifact] = Field(max_length=5)
    error: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _error_matches_success(self) -> "AgentReport":
        if not self.success and not self.error:
            raise ValueError("error is required when success is false")
        if self.success and self.error:
            raise ValueError("error must be null when success is true")
        return self


class AgentStep(BaseModel):
    """One turn of a tool-using agent: request a tool call, or finish with a report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["call_tool", "finish"]
    tool_call: ToolCall | None = None
    report: AgentReport | None = None

    @model_validator(mode="after")
    def _one_of(self) -> "AgentStep":
        """Exactly one of tool_call / report, matching `action`. Each violation is named,
        so a rejected step says what was wrong; any violation still rejects the step."""
        problems: list[str] = []
        if self.action == "call_tool":
            if self.tool_call is None:
                problems.append("tool_call is missing")
            if self.report is not None:
                problems.append("report must be null")
        else:
            if self.report is None:
                problems.append("report is missing")
            if self.tool_call is not None:
                problems.append("tool_call must be null")
        if problems:
            raise ValueError(f"action {self.action}: " + "; ".join(problems))
        return self


class AgentResult(AgentReport):
    # Set by the agent implementation (provider, model, token usage), never by the LLM.
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
