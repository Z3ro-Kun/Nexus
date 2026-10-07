"""Deterministic tool authorization: agent_type + tool_name -> allowed / denied.

Permissions are fixed by application configuration. Nothing an LLM outputs can change
them; a request for a tool outside the agent's set fails before any execution.
"""

from collections.abc import Iterable, Mapping

from app.tools.errors import ToolNotAuthorizedError

DEFAULT_TOOL_PERMISSIONS: dict[str, frozenset[str]] = {
    "planner": frozenset(),
    "researcher": frozenset({"web_search", "http_fetch"}),
    "analyst": frozenset({"calculator", "python_analysis"}),
    "specialist": frozenset({"calculator"}),  # configurable: NEXUS_SPECIALIST_TOOLS
}


class ToolPolicy:
    def __init__(self, permissions: Mapping[str, Iterable[str]] = DEFAULT_TOOL_PERMISSIONS) -> None:
        self._permissions = {agent: frozenset(tools) for agent, tools in permissions.items()}

    @classmethod
    def with_specialist_tools(cls, tools: Iterable[str]) -> "ToolPolicy":
        permissions = dict(DEFAULT_TOOL_PERMISSIONS)
        permissions["specialist"] = frozenset(tools)
        return cls(permissions)

    def allowed(self, agent_type: str) -> frozenset[str]:
        return self._permissions.get(agent_type, frozenset())

    def is_allowed(self, agent_type: str, tool_name: str) -> bool:
        return tool_name in self.allowed(agent_type)

    def authorize(self, agent_type: str, tool_name: str) -> None:
        if not self.is_allowed(agent_type, tool_name):
            raise ToolNotAuthorizedError(
                f"agent {agent_type!r} is not authorized to use tool {tool_name!r}"
            )
