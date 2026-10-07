"""tool name -> tool implementation. A tool that is not registered does not exist."""

from app.tools.base import Tool
from app.tools.errors import UnknownToolError
from app.tools.schemas import ToolDefinition


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        name = tool.definition.name
        if name in self._tools:
            raise ValueError(f"tool {name!r} is already registered")
        self._tools[name] = tool

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise UnknownToolError(f"unknown tool {name!r}")
        return tool

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def definitions(self) -> list[ToolDefinition]:
        return [self._tools[name].definition for name in sorted(self._tools)]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))
