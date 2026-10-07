"""Deterministic fake tools for tests and offline development.

Every fake identifies itself: its definition has `fake=True` (so ToolSucceeded metadata
and fact provenance say `fake`), its description says FAKE, and fake URLs use the
reserved `.invalid` top-level domain, which can never resolve to a real site.
None of these performs I/O, and FakeSandboxBackend never executes code.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel

from app.tools.calculator import CalculatorInput, CalculatorOutput
from app.tools.errors import ToolError, ToolExecutionError, ToolHTTPError
from app.tools.http_fetch import HTTPFetchInput, HTTPFetchOutput
from app.tools.network import check_url
from app.tools.python_analysis import PythonAnalysisOutput
from app.events.types import ActionCategory
from app.tools.schemas import ToolContext, ToolDefinition
from app.tools.web_search import SearchResult

FAKE_DOMAIN = "fake-search.invalid"


class FakeCalculator:
    """Returns scripted results; unknown expressions fail. (The real CalculatorTool is
    itself deterministic and offline; this fake exists for scripted-failure tests.)"""

    definition = ToolDefinition(
        name="calculator",
        description="FAKE calculator for tests.",
        capabilities="Returns scripted results only.",
        input_model=CalculatorInput,
        output_model=CalculatorOutput,
        risk_level="low",
        category=ActionCategory.READ_ONLY,
        timeout_seconds=2.0,
        fake=True,
    )

    def __init__(self, results: Mapping[str, float | int]) -> None:
        self._results = dict(results)

    async def execute(self, arguments: BaseModel, context: ToolContext) -> CalculatorOutput:
        assert isinstance(arguments, CalculatorInput)
        if arguments.expression not in self._results:
            raise ToolExecutionError(f"fake calculator has no result for {arguments.expression!r}")
        return CalculatorOutput(expression=arguments.expression, result=self._results[arguments.expression])


class FakeSearchBackend:
    """Scripted results per query, or generated placeholders on the .invalid domain.
    `gates` lets a test hold a search open (to observe concurrency). `failures` makes a
    query fail deterministically with the given tool error (e.g. to exercise recovery
    without depending on a real outage)."""

    name = "fake-search"
    fake = True

    def __init__(
        self,
        results: Mapping[str, list[SearchResult]] | None = None,
        gates: Mapping[str, asyncio.Event] | None = None,
        failures: Mapping[str, ToolError] | None = None,
    ) -> None:
        self._results = dict(results or {})
        self._gates = dict(gates or {})
        self._failures = dict(failures or {})
        self.queries: list[str] = []
        self.active: set[str] = set()
        self.max_active = 0
        self._started: dict[str, asyncio.Event] = {}

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        self.queries.append(query)
        self.active.add(query)
        self.max_active = max(self.max_active, len(self.active))
        self._started.setdefault(query, asyncio.Event()).set()
        try:
            if query in self._gates:
                await self._gates[query].wait()
        finally:
            self.active.discard(query)
        if query in self._failures:
            raise self._failures[query]
        if query in self._results:
            return self._results[query][:max_results]
        slug = "-".join(query.lower().split())[:60] or "query"
        return [
            SearchResult(
                title=f"[FAKE] Result {i} for {query}",
                url=f"https://{FAKE_DOMAIN}/{slug}/{i}",
                snippet=f"Fake search result {i} for '{query}'. Not real data.",
                source=self.name,
            )
            for i in range(1, max_results + 1)
        ]

    async def wait_until_searched(self, query: str) -> None:
        await self._started.setdefault(query, asyncio.Event()).wait()


@dataclass(frozen=True)
class FakePage:
    content: str
    status_code: int = 200
    content_type: str = "text/html; charset=utf-8"


class FakeHTTPFetch:
    """Serves scripted pages. Applies the same static URL policy as the real fetcher
    (scheme, port, blocked names, IP literals); it performs no DNS or network I/O."""

    definition = ToolDefinition(
        name="http_fetch",
        description="FAKE http_fetch for tests: serves scripted pages, no network.",
        capabilities="Scripted pages only.",
        input_model=HTTPFetchInput,
        output_model=HTTPFetchOutput,
        risk_level="high",
        category=ActionCategory.NETWORK_READ,
        timeout_seconds=5.0,
        fake=True,
    )

    def __init__(self, pages: Mapping[str, FakePage]) -> None:
        self._pages = dict(pages)

    async def execute(self, arguments: BaseModel, context: ToolContext) -> HTTPFetchOutput:
        assert isinstance(arguments, HTTPFetchInput)
        check_url(arguments.url)
        page = self._pages.get(arguments.url)
        if page is None:
            raise ToolHTTPError(f"HTTP 404 from {arguments.url} (fake)")
        if page.status_code >= 400:
            raise ToolHTTPError(f"HTTP {page.status_code} from {arguments.url} (fake)")
        return HTTPFetchOutput(
            url=arguments.url,
            final_url=arguments.url,
            status_code=page.status_code,
            content_type=page.content_type,
            content=page.content,
            content_bytes=len(page.content.encode()),
            redirects=[],
        )


class FakeSandboxBackend:
    """Returns scripted outputs keyed by the exact code string. Never executes code."""

    name = "fake-sandbox"
    fake = True

    def __init__(self, outputs: Mapping[str, PythonAnalysisOutput]) -> None:
        self._outputs = dict(outputs)
        self.received: list[str] = []

    async def run(self, code: str, data_json: str | None) -> PythonAnalysisOutput:
        self.received.append(code)
        if code not in self._outputs:
            raise ToolExecutionError("fake sandbox has no scripted output for this code")
        return self._outputs[code]


class SideEffectInput(BaseModel):
    model_config = {"extra": "forbid"}

    item: str
    quantity: int = 1


class SideEffectOutput(BaseModel):
    model_config = {"extra": "forbid"}

    receipt_id: str
    item: str
    quantity: int
    effect: str


class FakeSideEffectTool:
    """A FAKE consequential action (e.g. "place an order") for Phase 8 tests. It changes
    nothing outside the process; it counts how often it really executed, so tests can
    prove that the policy gate sits before execution. Never registered in production."""

    def __init__(self, name: str = "place_order", category: ActionCategory = ActionCategory.IRREVERSIBLE) -> None:
        self.executions: list[dict[str, object]] = []
        self.definition = ToolDefinition(
            name=name,
            description=f"FAKE {name}: records a simulated {category.value} effect; no real side effect.",
            capabilities="Simulated side effect for tests only.",
            input_model=SideEffectInput,
            output_model=SideEffectOutput,
            risk_level="high",
            category=category,
            timeout_seconds=2.0,
            fake=True,
        )

    @property
    def count(self) -> int:
        return len(self.executions)

    async def execute(self, arguments: BaseModel, context: ToolContext) -> SideEffectOutput:
        assert isinstance(arguments, SideEffectInput)
        self.executions.append({"task_id": context.task_id, "item": arguments.item, "quantity": arguments.quantity})
        return SideEffectOutput(
            receipt_id=f"fake-{self.definition.name}-{self.count}", item=arguments.item,
            quantity=arguments.quantity, effect=f"simulated {self.definition.category.value}",
        )
