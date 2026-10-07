"""Python / data analysis tool over a pluggable sandbox backend.

Security model: LLM-written code must never run in the backend process, and must only run
in an isolated sandbox that provides, at minimum, a strict CPU/wall-clock timeout,
memory and output limits, no network, no access to the host filesystem, and no ability to
spawn processes on the host.

No such sandbox is available in this environment (Windows host, no container runtime),
and a plain subprocess would not meet those requirements. So:

- there is NO real backend; nothing in NEXUS executes model-written code;
- the tool is registered only when a backend is supplied, so by default agents never
  see it;
- tests use `FakeSandboxBackend` (app.tools.fakes), which returns scripted outputs and
  never executes the code it receives.
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.events.types import ActionCategory
from app.tools.schemas import ToolContext, ToolDefinition


class PythonAnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=10_000)
    # Input data as a JSON document (structured outputs cannot carry arbitrary objects).
    data_json: str | None = Field(default=None, max_length=100_000)


class PythonAnalysisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stdout: str = Field(max_length=20_000)
    result: JsonValue = None
    backend: str


class SandboxBackend(Protocol):
    name: str
    fake: bool

    async def run(self, code: str, data_json: str | None) -> PythonAnalysisOutput: ...


class PythonAnalysisTool:
    def __init__(self, backend: SandboxBackend) -> None:
        self._backend = backend
        label = " (FAKE sandbox: code is not executed)" if backend.fake else ""
        self.definition = ToolDefinition(
            name="python_analysis",
            description="Run a short Python data-analysis snippet in an isolated sandbox." + label,
            capabilities=(
                f"Sandbox: {backend.name}. No network, no host filesystem, strict time and "
                "memory limits. Print results or set `result`."
            ),
            input_model=PythonAnalysisInput,
            output_model=PythonAnalysisOutput,
            risk_level="high",
            category=ActionCategory.READ_ONLY,
            timeout_seconds=30.0,
            fake=backend.fake,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> PythonAnalysisOutput:
        assert isinstance(arguments, PythonAnalysisInput)
        return await self._backend.run(arguments.code, arguments.data_json)
