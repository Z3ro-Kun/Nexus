"""Task executor interface, and a scripted executor for exercising the scheduler.

The scheduler decides *when* a task runs; an executor performs it and reports a result.
Executors never write events or touch state: the scheduler records their results.

Implementations: `ScriptedTaskExecutor` (below) performs no work; each task's outcome is
fixed in advance by the caller, to test the scheduler deterministically. It is not an
agent. `app.agents.runtime.AgentTaskExecutor` runs tasks with LLM-backed agents.
"""

import asyncio
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SerializeAsAny

from app.events.types import EventPayload, EventType, Evidence
from app.state.context_builder import TaskContext
from app.state.models import TaskState

# The only event types an executor may ask the scheduler to record. Lifecycle, run and
# planning events are reserved for NEXUS itself.
# - result events: recorded only with TaskCompleted;
# - tool events: recorded with TaskCompleted or TaskFailed (tool use is always observable).
RESULT_EVENT_TYPES = frozenset({
    EventType.FACT_ADDED,
    EventType.ARTIFACT_ADDED,
    # Phase 10: workspace artifacts, validated and packaged by the agent runtime.
    EventType.ARTIFACT_CREATED,
    EventType.ARTIFACT_FILE_ADDED,
    EventType.ARTIFACT_VALIDATED,
    EventType.ARTIFACT_PACKAGED,
})
TOOL_EVENT_TYPES = frozenset(
    {EventType.TOOL_CALLED, EventType.TOOL_SUCCEEDED, EventType.TOOL_FAILED}
)


class TaskExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    succeeded: bool
    summary: str | None = None
    error: str | None = None
    # Recorded atomically with the outcome. On success: RESULT_EVENT_TYPES and
    # TOOL_EVENT_TYPES; on failure: TOOL_EVENT_TYPES only.
    events: list[SerializeAsAny[EventPayload]] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    # Recorded as the envelope agent_id of the events written for this outcome.
    agent_id: str | None = None
    # Failure details for classification (Phase 5, app.recovery.classifier): a stable
    # cause such as "agent_timeout" or "tool_failed", and the failed tool call, if any.
    error_type: str | None = None
    tool_call_id: str | None = None

    @classmethod
    def success(cls, summary: str | None = None) -> "TaskExecutionResult":
        return cls(succeeded=True, summary=summary)

    @classmethod
    def failure(cls, error: str, error_type: str | None = None) -> "TaskExecutionResult":
        return cls(succeeded=False, error=error, error_type=error_type)

    def allowed_event_types(self) -> frozenset[EventType]:
        return RESULT_EVENT_TYPES | TOOL_EVENT_TYPES if self.succeeded else TOOL_EVENT_TYPES


class TaskExecutor(Protocol):
    async def execute(self, task: TaskState, context: TaskContext) -> TaskExecutionResult: ...


class Outcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True)
class Script:
    """How the scripted executor handles one task.

    If `wait_for` is set (WAIT_FOR_SIGNAL), execution blocks until that event is set.
    `delay_seconds` then adds a fixed wait. Finally `outcome` is reported.
    """

    outcome: Outcome = Outcome.SUCCESS
    wait_for: asyncio.Event | None = None
    delay_seconds: float = 0.0


class ScriptedTaskExecutor:
    """Deterministic executor whose per-task outcomes are fixed in advance.

    It also records how it was called, so tests can check the scheduler's behavior:
    `calls` (executions per task), `start_order`, and `max_active` (the most tasks
    executing at the same time).
    """

    def __init__(
        self, scripts: Mapping[str, Script] | None = None, default: Script = Script()
    ) -> None:
        self._scripts = dict(scripts or {})
        self._default = default
        self.calls: Counter[str] = Counter()
        self.start_order: list[str] = []
        self.active: set[str] = set()
        self.max_active = 0
        self._started: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)

    async def execute(self, task: TaskState, context: TaskContext) -> TaskExecutionResult:
        script = self._scripts.get(task.task_id, self._default)
        self.calls[task.task_id] += 1
        self.start_order.append(task.task_id)
        self.active.add(task.task_id)
        self.max_active = max(self.max_active, len(self.active))
        self._started[task.task_id].set()
        try:
            if script.wait_for is not None:
                await script.wait_for.wait()
            if script.delay_seconds:
                await asyncio.sleep(script.delay_seconds)
        finally:
            self.active.discard(task.task_id)

        if script.outcome is Outcome.FAILURE:
            return TaskExecutionResult.failure(f"scripted failure of task {task.task_id!r}")
        return TaskExecutionResult.success(f"scripted success of task {task.task_id!r}")

    async def wait_until_started(self, task_id: str) -> None:
        await self._started[task_id].wait()
