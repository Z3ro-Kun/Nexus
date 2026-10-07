"""Per-task tool access for agents.

The runtime creates one `ToolSession` per task and hands it to the agent. The session is
the agent's only way to use tools. It:
- exposes only tools that are registered AND authorized for the agent type;
- enforces the per-task limit (`max_calls`) before anything runs;
- assigns tool call ids (`<task_id>.t<n>`) and records every request and its result;
- ends the task on the first failed call (`ToolCallFailedError`); tools are never retried.

The runtime reads `trace` afterwards, even if the agent timed out or raised, so every
requested tool call becomes ToolCalled + ToolSucceeded/ToolFailed events.
"""

from datetime import datetime, timezone
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue

from app.tools.executor import ToolExecutor
from app.tools.schemas import ToolCall, ToolContext, ToolDefinition, ToolResult


class ToolTraceEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_call_id: str
    tool_name: str
    arguments: dict[str, JsonValue]
    requested_at: datetime
    completed_at: datetime | None = None
    result: ToolResult | None = None  # None: interrupted before completion


class ToolLimitExceededError(Exception):
    pass


class ToolCallFailedError(Exception):
    def __init__(self, entry: ToolTraceEntry) -> None:
        assert entry.result is not None
        super().__init__(
            f"tool call {entry.tool_call_id} ({entry.tool_name}) failed "
            f"[{entry.result.error_type}]: {entry.result.error}"
        )
        self.entry = entry


class ToolSession:
    def __init__(
        self,
        executor: ToolExecutor,
        *,
        run_id: UUID,
        task_id: str,
        agent_type: str,
        max_calls: int,
    ) -> None:
        self._executor = executor
        self._run_id = run_id
        self._task_id = task_id
        self._agent_type = agent_type
        self.max_calls = max_calls
        self.definitions: list[ToolDefinition] = executor.available_tools(agent_type)
        self.trace: list[ToolTraceEntry] = []

    async def call(self, call: ToolCall) -> ToolTraceEntry:
        if len(self.trace) >= self.max_calls:
            raise ToolLimitExceededError(f"tool-call limit of {self.max_calls} per task reached")
        index = len(self.trace)
        entry = ToolTraceEntry(
            tool_call_id=f"{self._task_id}.t{index + 1}",
            tool_name=call.tool_name,
            arguments=self._recorded_arguments(call),
            requested_at=datetime.now(timezone.utc),
        )
        self.trace.append(entry)
        result = await self._executor.execute(
            call,
            ToolContext(
                run_id=self._run_id,
                task_id=self._task_id,
                agent_type=self._agent_type,
                tool_call_id=entry.tool_call_id,
            ),
        )
        entry = entry.model_copy(update={"completed_at": datetime.now(timezone.utc), "result": result})
        self.trace[index] = entry
        if not result.success:
            raise ToolCallFailedError(entry)
        return entry

    def _recorded_arguments(self, call: ToolCall) -> dict[str, JsonValue]:
        """The arguments as recorded in events: the tool may replace bulky or sensitive
        values (e.g. file content) with a digest. The tool itself gets the full call."""
        arguments = dict(call.arguments)
        if call.tool_name not in self._executor.registry:
            return arguments
        recorder = self._executor.registry.get(call.tool_name).definition.record_arguments
        return recorder(arguments) if recorder is not None else arguments
