"""The agent interface.

An agent turns a `TaskContext` (a read-only projection of run state) into an
`AgentResult`. It has no database, session, repository or event-store access: it cannot
change persistent state. The runtime decides what, if anything, of its result is recorded.
"""

from typing import Protocol

from app.agents.result import AgentResult
from app.agents.tooling import ToolSession
from app.state.context_builder import TaskContext

# The agent's input is the Phase 1 task context projection.
AgentContext = TaskContext


class Agent(Protocol):
    agent_type: str

    # `tools` is the agent's only route to tools; None means no tool access.
    async def run(self, context: AgentContext, tools: ToolSession | None = None) -> AgentResult: ...
