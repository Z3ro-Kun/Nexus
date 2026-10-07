"""The tool interface.

A tool receives already-validated arguments (an instance of its input model) and a
minimal `ToolContext`, and returns an instance of its output model or raises a
`ToolError`. Tools have no access to NEXUS state or the database; their output goes back
to the agent runtime, which decides what (if anything) becomes an event.
"""

from typing import Protocol

from pydantic import BaseModel

from app.tools.schemas import ToolContext, ToolDefinition


class Tool(Protocol):
    definition: ToolDefinition

    async def execute(self, arguments: BaseModel, context: ToolContext) -> BaseModel: ...
