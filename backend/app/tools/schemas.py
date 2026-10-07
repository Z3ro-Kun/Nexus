"""Tool call, tool result and tool definition types."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.events.types import ActionCategory
from app.llm.schemas import strict_json_schema

RiskLevel = Literal["low", "medium", "high"]


class ToolCall(BaseModel):
    """A tool request as proposed by an LLM. Untrusted until the executor validates it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, JsonValue]


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    success: bool
    output: dict[str, JsonValue] | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    error: str | None = None
    error_type: str | None = None


class ToolContext(BaseModel):
    """What a tool is told about its caller. Deliberately no state, session or database."""

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    task_id: str
    agent_type: str
    tool_call_id: str


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str  # shown to the LLM
    capabilities: str  # what the tool can and cannot do, shown to the LLM
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    risk_level: RiskLevel
    # Phase 8: what executing the tool can do (authoritative input to the policy engine).
    # Required, so no tool is ever classified by default or by model output.
    category: ActionCategory
    timeout_seconds: float
    # True for deterministic stand-ins. Their output is marked fake in metadata and
    # provenance and must never be presented as real-world information.
    fake: bool = False
    # Only for agents' tool loops: never offered to the planner as an action task.
    agent_only: bool = False
    # What ToolCalled records instead of the raw arguments (e.g. file content replaced by
    # its size and checksum). None: the arguments as given. Must not raise.
    record_arguments: Callable[[dict[str, JsonValue]], dict[str, JsonValue]] | None = None
    # How to plan work that uses this tool, shown to the planner and replanner with the
    # description (agents see `description` and `capabilities`). Empty: description only.
    planning_note: str = ""

    @property
    def input_schema(self) -> dict[str, Any]:
        return strict_json_schema(self.input_model)

    @property
    def output_schema(self) -> dict[str, Any]:
        return self.output_model.model_json_schema()
