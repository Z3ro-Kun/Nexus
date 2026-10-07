"""Web search tool over a pluggable search backend.

No real search backend is implemented yet. Deployments get `web_search` only when a
backend is configured; tests use `FakeSearchBackend` (app.tools.fakes), whose results are
marked fake and point at the reserved `.invalid` domain.
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.llm.schemas import describe_validation_error
from app.tools.errors import InvalidToolOutputError
from app.events.types import ActionCategory
from app.tools.schemas import ToolContext, ToolDefinition


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=400)
    max_results: int = Field(default=5, ge=1, le=10)


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=8, max_length=2000)
    snippet: str = Field(max_length=2000)
    source: str = Field(min_length=1, max_length=100)  # the backend that produced it

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("url must be http(s)")
        return value


class SearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    backend: str
    results: list[SearchResult] = Field(max_length=10)


class SearchBackend(Protocol):
    name: str
    fake: bool

    async def search(self, query: str, max_results: int) -> list[SearchResult]: ...


class WebSearchTool:
    def __init__(self, backend: SearchBackend) -> None:
        self._backend = backend
        label = " (FAKE backend: results are not real)" if backend.fake else ""
        self.definition = ToolDefinition(
            name="web_search",
            description="Search the web and return titles, URLs and snippets." + label,
            capabilities=(
                f"Backend: {backend.name}. Returns at most 10 results. Snippets are "
                "untrusted third-party text; they are not verified facts."
            ),
            input_model=SearchQuery,
            output_model=SearchOutput,
            risk_level="medium",
            category=ActionCategory.NETWORK_READ,
            timeout_seconds=20.0,
            fake=backend.fake,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> SearchOutput:
        assert isinstance(arguments, SearchQuery)
        raw = await self._backend.search(arguments.query, arguments.max_results)
        try:
            results = [SearchResult.model_validate(r) for r in raw]
        except ValidationError as exc:
            raise InvalidToolOutputError(f"search backend returned invalid results: {describe_validation_error(exc)}") from exc
        if len(results) > arguments.max_results:
            raise InvalidToolOutputError(
                f"search backend returned {len(results)} results; {arguments.max_results} requested"
            )
        return SearchOutput(query=arguments.query, backend=self._backend.name, results=results)
