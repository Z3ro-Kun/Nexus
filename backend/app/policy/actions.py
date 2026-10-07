"""Executes an authorized action task: exactly its predeclared tool call. No LLM.

    action task + Authorization (built from recorded state) -> ToolExecutor (policy gate
    again, then the tool) -> ToolCalled/ToolSucceeded + a tool-derived fact -> result

The scheduler calls this only after the gate let the task start, and records the result
like any task's. The ToolExecutor re-checks the authorization against the exact tool and
arguments, so this path cannot execute anything that was not decided on.
"""

import json
from datetime import datetime, timezone
from uuid import UUID

from app.agents.result import AgentFact
from app.agents.runtime import fact_provenance, tool_events
from app.agents.tooling import ToolTraceEntry
from app.events.types import ACTION_AGENT_TYPE, Evidence, FactAdded
from app.orchestration.task_executor import TaskExecutionResult
from app.policy.engine import Authorization
from app.state.models import TaskState
from app.tools.executor import ToolExecutor
from app.tools.schemas import ToolCall, ToolContext


class ActionTaskExecutor:
    def __init__(self, tool_executor: ToolExecutor) -> None:
        self.tool_executor = tool_executor

    async def execute(self, run_id: UUID, task: TaskState, authorization: Authorization) -> TaskExecutionResult:
        assert task.action is not None
        action = task.action
        call_id = f"{task.task_id}.t1"
        requested = datetime.now(timezone.utc)
        result = await self.tool_executor.execute(
            ToolCall(tool_name=action.tool_name, arguments=action.arguments),
            ToolContext(run_id=run_id, task_id=task.task_id, agent_type=ACTION_AGENT_TYPE, tool_call_id=call_id),
            authorization,
        )
        entry = ToolTraceEntry(
            tool_call_id=call_id, tool_name=action.tool_name, arguments=dict(action.arguments),
            requested_at=requested, completed_at=datetime.now(timezone.utc), result=result,
        )
        events = tool_events([entry])
        if not result.success:
            return TaskExecutionResult(
                succeeded=False, error=f"action {action.tool_name} failed [{result.error_type}]: {result.error}",
                error_type="tool_failed", tool_call_id=call_id, events=events, agent_id=ACTION_AGENT_TYPE,
            )
        output = json.dumps(result.output, ensure_ascii=False, sort_keys=True)
        fact = AgentFact(content=f"Action {action.tool_name} executed: {output}"[:2000], basis="tool_output", tool_call_id=call_id)
        provenance = fact_provenance(fact, [entry])
        approval = f", approval {authorization.approval_id}" if authorization.approval_id else ""
        return TaskExecutionResult(
            succeeded=True,
            summary=f"Executed {action.tool_name} as authorized ({authorization.decision.rule}{approval}).",
            events=[
                *events,
                FactAdded(fact_id=f"{task.task_id}.f1", content=fact.content, source=provenance.source, provenance=provenance),
            ],
            evidence=[Evidence(source="tool_output", reference=call_id, note=action.intent[:1000])],
            metadata={
                "policy_outcome": authorization.decision.outcome.value,
                "policy_rule": authorization.decision.rule,
                "approval_id": authorization.approval_id,
            },
            agent_id=ACTION_AGENT_TYPE,
        )
