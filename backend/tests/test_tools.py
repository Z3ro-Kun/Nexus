"""Tool system unit tests: registry, policy, validation, calculator, HTTP/SSRF, search,
python analysis. No network: HTTP uses httpx.MockTransport and a fake DNS resolver."""

import ast
import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.events.types import ActionCategory
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.tools.calculator import CalculatorInput, CalculatorOutput, CalculatorTool, evaluate
from app.tools.errors import (
    InvalidToolArgumentsError,
    ToolExecutionError,
    UnknownToolError,
)
from app.tools.executor import ToolExecutor
from app.tools.factory import build_tool_policy, build_tool_registry
from app.tools.fakes import FakeCalculator, FakeSandboxBackend, FakeSearchBackend
from app.tools.http_fetch import HTTPFetchTool
from app.tools.policy import DEFAULT_TOOL_PERMISSIONS, ToolPolicy
from app.tools.python_analysis import PythonAnalysisOutput, PythonAnalysisTool
from app.tools.registry import ToolRegistry
from app.tools.schemas import ToolCall, ToolContext, ToolDefinition
from app.tools.web_search import SearchResult, WebSearchTool

APP_TOOLS = Path(__file__).resolve().parents[1] / "app" / "tools"


def ctx(agent_type: str = "analyst") -> ToolContext:
    return ToolContext(run_id=uuid4(), task_id="t1", agent_type=agent_type, tool_call_id="t1.t1")


async def run(executor: ToolExecutor, tool_name: str, agent_type: str = "analyst", **arguments: object):  # type: ignore[no-untyped-def]
    return await executor.execute(ToolCall(tool_name=tool_name, arguments=arguments), ctx(agent_type))  # type: ignore[arg-type]


class SpyTool:
    """Records whether execute() was ever reached."""

    def __init__(self, name: str = "calculator", delay: float = 0, output: object = None) -> None:
        self.definition = ToolDefinition(
            name=name, description="spy", capabilities="spy", input_model=CalculatorInput,
            output_model=CalculatorOutput, risk_level="low", category=ActionCategory.READ_ONLY, timeout_seconds=0.2, fake=True,
        )
        self.executed = 0
        self._delay = delay
        self._output = output

    async def execute(self, arguments: BaseModel, context: ToolContext) -> object:
        self.executed += 1
        await asyncio.sleep(self._delay)
        return self._output if self._output is not None else CalculatorOutput(expression="1", result=1)


# --- Registry ------------------------------------------------------------------------------


def test_registry_register_resolve_list_and_reject_unknown() -> None:
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    registry.register(WebSearchTool(FakeSearchBackend()))

    assert isinstance(registry.get("calculator"), CalculatorTool)
    assert registry.names == ("calculator", "web_search")
    assert [d.name for d in registry.definitions()] == ["calculator", "web_search"]
    with pytest.raises(UnknownToolError):
        registry.get("shell")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(CalculatorTool())


def test_production_registry_never_contains_fakes_or_unsandboxed_python() -> None:
    default = build_tool_registry(Settings(_env_file=None))  # type: ignore[call-arg]
    assert default.names == ("calculator",)

    enabled = build_tool_registry(Settings(_env_file=None, NEXUS_HTTP_FETCH_ENABLED=True))  # type: ignore[call-arg]
    assert enabled.names == ("calculator", "http_fetch")
    assert not any(d.fake for d in enabled.definitions())


# --- Policy --------------------------------------------------------------------------------


def test_default_permissions_are_per_agent() -> None:
    policy = ToolPolicy()
    assert policy.allowed("researcher") == {"web_search", "http_fetch"}
    assert policy.allowed("analyst") == {"calculator", "python_analysis"}
    assert policy.allowed("specialist") == {"calculator"}
    assert policy.allowed("planner") == frozenset()
    assert policy.allowed("unknown_agent") == frozenset()
    assert not policy.is_allowed("researcher", "calculator")


def test_specialist_tools_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_SPECIALIST_TOOLS", '["calculator", "http_fetch"]')
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert build_tool_policy(settings).allowed("specialist") == {"calculator", "http_fetch"}
    monkeypatch.setenv("NEXUS_SPECIALIST_TOOLS", '["shell"]')
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
    assert DEFAULT_TOOL_PERMISSIONS["specialist"] == {"calculator"}


# --- Validation before execution -----------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [{"tool_name": "calculator"}, {"arguments": {}}, {"tool_name": "", "arguments": {}}, {"tool_name": "calculator", "arguments": {}, "run_as": "root"}],
)
def test_malformed_tool_call_is_rejected(data: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValidationError):
        ToolCall.model_validate(data)


@pytest.mark.parametrize(
    ("tool", "agent", "arguments", "error_type"),
    [
        ("shell", "analyst", {"cmd": "ls"}, "unknown_tool"),
        ("calculator", "researcher", {"expression": "1+1"}, "unauthorized"),
        ("calculator", "analyst", {"expr": "1+1"}, "invalid_arguments"),
        ("calculator", "analyst", {"expression": "1+1", "extra": True}, "invalid_arguments"),
        ("calculator", "analyst", {"expression": "x" * 501}, "invalid_arguments"),
    ],
    ids=["unknown", "unauthorized", "wrong-argument", "extra-argument", "input-limit"],
)
async def test_invalid_calls_fail_before_execution(tool: str, agent: str, arguments: dict, error_type: str) -> None:  # type: ignore[type-arg]
    spy = SpyTool()
    executor = ToolExecutor(ToolRegistry([spy]), ToolPolicy())

    result = await run(executor, tool, agent, **arguments)

    assert not result.success and result.error_type == error_type
    assert spy.executed == 0


async def test_tool_timeout_and_invalid_output_become_failures() -> None:
    slow = ToolExecutor(ToolRegistry([SpyTool(delay=5)]), ToolPolicy())
    result = await run(slow, "calculator", expression="1")
    assert (result.success, result.error_type) == (False, "timeout")

    bad = ToolExecutor(ToolRegistry([SpyTool(output={"unexpected": 1})]), ToolPolicy())
    result = await run(bad, "calculator", expression="1")
    assert (result.success, result.error_type) == (False, "invalid_output")


async def test_successful_result_is_structured_and_labelled() -> None:
    executor = ToolExecutor(ToolRegistry([CalculatorTool()]), ToolPolicy())

    result = await run(executor, "calculator", expression="(1299 - 999) / 999 * 100")

    assert result.success and result.error is None
    assert result.output == {"expression": "(1299 - 999) / 999 * 100", "result": pytest.approx(30.03003)}
    assert result.metadata["fake"] is False and result.metadata["risk_level"] == "low"
    assert result.metadata["duration_ms"] >= 0


# --- Calculator ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [("1 + 2 * 3", 7), ("(1 + 2) * 3", 9), ("2 ** 10", 1024), ("7 // 2", 3), ("7 % 4", 3), ("-3 + +2", -1),
     ("sqrt(16) + abs(-2)", 6.0), ("max(1, 5, 3) - min(4, 2)", 3), ("round(2.567, 2)", 2.57), ("pi * 2", 6.283185307179586)],
)
def test_calculator_valid_arithmetic(expression: str, expected: float) -> None:
    assert evaluate(expression) == pytest.approx(expected)


@pytest.mark.parametrize("expression", ["", "1 +", "(1", "1 2", "1 +* 2"])
def test_calculator_invalid_expression(expression: str) -> None:
    with pytest.raises(InvalidToolArgumentsError):
        evaluate(expression)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo pwned')",
        "open('/etc/passwd').read()",
        "().__class__.__bases__[0].__subclasses__()",
        "eval('1+1')",
        "exec('x=1')",
        "globals()",
        "(lambda: 1)()",
        "[x for x in range(10)]",
        "'a' * 10",
        "True + 1",
        "x",
        "math.sqrt(4)",
        "sqrt(key=4)",
        "sqrt(*[4])",
        "a := 1",
        "9 ** 99999",
        "1" + " + 1" * 300,
    ],
)
def test_calculator_rejects_malicious_or_unsupported_expressions(expression: str) -> None:
    with pytest.raises(InvalidToolArgumentsError):
        evaluate(expression)


@pytest.mark.parametrize("expression", ["1 / 0", "sqrt(-1)", "10 ** 200", "exp(1000)", "(-8) ** 0.5"])
def test_calculator_math_errors(expression: str) -> None:
    with pytest.raises(ToolExecutionError):
        evaluate(expression)


def test_calculator_never_uses_eval_or_exec() -> None:
    tree = ast.parse((APP_TOOLS / "calculator.py").read_text(encoding="utf-8"))
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not called & {"eval", "exec", "compile", "__import__"}


# --- HTTP fetch + SSRF (fake DNS, mock transport: no network) ------------------------------


class FakeResolver:
    def __init__(self, records: dict[str, list[str]]) -> None:
        self.records = records
        self.lookups: list[str] = []

    async def resolve(self, host: str, port: int) -> list[str]:
        self.lookups.append(host)
        return self.records.get(host, [])


PUBLIC = "93.184.216.34"


def fetcher(handler, records: dict[str, list[str]] | None = None, **kwargs: object) -> tuple[ToolExecutor, list[httpx.Request], FakeResolver]:  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    async def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return await handler(request) if asyncio.iscoroutinefunction(handler) else handler(request)

    resolver = FakeResolver(records if records is not None else {"example.com": [PUBLIC]})
    tool = HTTPFetchTool(resolver=resolver, transport=httpx.MockTransport(recording), **kwargs)  # type: ignore[arg-type]
    return ToolExecutor(ToolRegistry([tool]), ToolPolicy()), seen, resolver


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=b"<p>hello</p>")


async def fetch(executor: ToolExecutor, url: str, **kwargs: object):  # type: ignore[no-untyped-def]
    return await run(executor, "http_fetch", "researcher", url=url, **kwargs)


def test_http_fetch_capabilities_state_the_timeout_limit() -> None:
    capabilities = HTTPFetchTool(max_bytes=1024, max_timeout_seconds=15).definition.capabilities
    assert "timeout_seconds is optional, defaults to 10, and must be at most 30; omit it unless needed." in capabilities


async def test_timeout_above_the_limit_is_rejected_before_any_request() -> None:
    executor, seen, _ = fetcher(ok)

    result = await fetch(executor, "https://example.com", timeout_seconds=60)

    assert (result.success, result.error_type) == (False, "invalid_arguments")
    assert "timeout_seconds" in (result.error or "") and seen == []  # nothing was fetched
    assert (await fetch(executor, "https://example.com", timeout_seconds=30)).success


async def test_allowed_url_is_fetched_via_the_validated_ip() -> None:
    executor, seen, resolver = fetcher(ok)

    result = await fetch(executor, "https://example.com/page", params=[{"name": "q", "value": "a b"}])

    assert result.success, result.error
    assert result.output["status_code"] == 200 and result.output["content"] == "<p>hello</p>"
    assert result.output["final_url"] == "https://example.com/page?q=a+b"
    assert result.output["content_type"].startswith("text/html")
    [request] = seen
    assert request.url.host == PUBLIC  # pinned: no second DNS lookup by the client
    assert request.headers["host"] == "example.com"
    assert request.extensions["sni_hostname"] == "example.com"  # TLS still verifies the name
    assert resolver.lookups == ["example.com"]


async def test_query_string_in_the_url_is_kept() -> None:
    """Regression: an API URL's own query string was dropped (httpx.URL(url, params=[])
    replaces it), so e.g. a Wikipedia API call silently fetched the API help page."""
    executor, seen, _ = fetcher(ok)
    url = "https://example.com/w/api.php?action=query&titles=Go_(programming_language)"

    result = await fetch(executor, url)
    assert result.success, result.error
    [request] = seen
    assert request.url.query == b"action=query&titles=Go_(programming_language)"
    assert result.output["url"] == result.output["final_url"] == url

    merged = await fetch(executor, url, params=[{"name": "format", "value": "json"}])
    assert merged.success, merged.error
    assert seen[1].url.params.multi_items() == [("action", "query"), ("titles", "Go_(programming_language)"), ("format", "json")]


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/", "http://LOCALHOST./", "http://api.localhost/", "http://127.0.0.1/", "http://127.1.2.3/",
        "http://[::1]/", "http://0.0.0.0/",
    ],
)
async def test_localhost_and_loopback_rejected(url: str) -> None:
    executor, seen, _ = fetcher(ok)
    result = await fetch(executor, url)
    assert result.error_type == "ssrf_blocked" and seen == []


@pytest.mark.parametrize(
    "url",
    ["http://10.0.0.5/", "http://172.16.1.1/", "http://192.168.1.1/", "http://[fd00::1]/", "http://[::ffff:192.168.0.1]/", "http://100.100.100.200/"],
)
async def test_private_addresses_rejected(url: str) -> None:
    executor, seen, _ = fetcher(ok)
    result = await fetch(executor, url)
    assert result.error_type == "ssrf_blocked" and seen == []


@pytest.mark.parametrize(
    "url",
    ["http://169.254.169.254/latest/meta-data/", "http://[fe80::1]/", "http://metadata.google.internal/", "http://instance-data/"],
)
async def test_link_local_and_metadata_rejected(url: str) -> None:
    executor, seen, _ = fetcher(ok)
    result = await fetch(executor, url)
    assert result.error_type == "ssrf_blocked" and seen == []


@pytest.mark.parametrize(
    "records",
    [{"evil.example": ["127.0.0.1"]}, {"evil.example": ["10.0.0.1"]}, {"evil.example": [PUBLIC, "169.254.169.254"]}, {"evil.example": ["::ffff:127.0.0.1"]}],
    ids=["dns-loopback", "dns-private", "one-bad-record", "dns-mapped-v6"],
)
async def test_hostnames_are_resolved_before_the_decision(records: dict) -> None:  # type: ignore[type-arg]
    executor, seen, _ = fetcher(ok, records)
    result = await fetch(executor, "https://evil.example/")
    assert result.error_type == "ssrf_blocked" and seen == []


@pytest.mark.parametrize(
    "url",
    ["ftp://example.com/", "file:///etc/passwd", "gopher://example.com/", "http://example.com:8080/", "https://example.com:22/", "http://user:pass@example.com/"],
)
async def test_schemes_ports_and_credentials_restricted(url: str) -> None:
    executor, seen, _ = fetcher(ok)
    result = await fetch(executor, url)
    assert result.error_type in {"ssrf_blocked", "invalid_arguments"} and seen == []


async def test_redirect_to_private_address_is_blocked() -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})

    executor, seen, _ = fetcher(redirect)
    result = await fetch(executor, "https://example.com/")
    assert result.error_type == "ssrf_blocked" and len(seen) == 1  # the private hop never happens


async def test_redirects_are_followed_and_bounded() -> None:
    def chain(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/final":
            return ok(request)
        return httpx.Response(301, headers={"location": "/final"})

    executor, seen, _ = fetcher(chain)
    result = await fetch(executor, "https://example.com/start")
    assert result.success and result.output["final_url"] == "https://example.com/final"
    assert result.output["redirects"] == ["https://example.com/final"]

    def loop(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/again"})

    executor, seen, _ = fetcher(loop)
    result = await fetch(executor, "https://example.com/")
    assert result.error_type == "http_error" and len(seen) == 4


@pytest.mark.parametrize("exc", [httpx.ConnectTimeout, httpx.ReadTimeout])
async def test_timeout_handled(exc: type[httpx.TimeoutException]) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise exc("slow", request=request)

    executor, _, _ = fetcher(timeout)
    result = await fetch(executor, "https://example.com/")
    assert result.error_type == "timeout"


async def test_network_error_handled() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    executor, _, _ = fetcher(refused)
    assert (await fetch(executor, "https://example.com/")).error_type == "network_error"


async def test_oversized_response_rejected() -> None:
    def declared(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 5000)

    executor, _, _ = fetcher(declared, max_bytes=1024)
    assert (await fetch(executor, "https://example.com/")).error_type == "size_limit"

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(100):
            yield b"y" * 100

    def streamed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=chunks())

    executor, _, _ = fetcher(streamed, max_bytes=1024)
    assert (await fetch(executor, "https://example.com/")).error_type == "size_limit"


async def test_http_error_status_and_binary_content() -> None:
    executor, _, _ = fetcher(lambda r: httpx.Response(404, content=b"missing"))
    assert (await fetch(executor, "https://example.com/")).error_type == "http_error"

    executor, _, _ = fetcher(lambda r: httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG"))
    result = await fetch(executor, "https://example.com/logo.png")
    assert result.success and result.output["content"] is None and result.output["content_bytes"] == 4


async def test_only_get_and_safe_headers() -> None:
    executor, seen, _ = fetcher(ok)
    for bad in ({"method": "POST"}, {"headers": [{"name": "Authorization", "value": "x"}]}, {"headers": [{"name": "Host", "value": "internal"}]}):
        result = await fetch(executor, "https://example.com/", **bad)
        assert result.error_type == "invalid_arguments"
    assert seen == []


# --- Web search ----------------------------------------------------------------------------


async def test_fake_search_works_and_is_labelled_fake() -> None:
    tool = WebSearchTool(FakeSearchBackend())
    executor = ToolExecutor(ToolRegistry([tool]), ToolPolicy())

    result = await run(executor, "web_search", "researcher", query="best laptops", max_results=3)

    assert result.success and result.metadata["fake"] is True
    assert tool.definition.fake and "FAKE" in tool.definition.description
    results = result.output["results"]
    assert len(results) == 3
    assert all(r["url"].startswith("https://fake-search.invalid/") and r["source"] == "fake-search" for r in results)
    assert all(SearchResult.model_validate(r) for r in results)


async def test_search_results_are_validated() -> None:
    class BadBackend:
        name, fake = "bad", True

        async def search(self, query: str, max_results: int) -> list[dict]:  # type: ignore[type-arg]
            return [{"title": "x", "url": "javascript:alert(1)", "snippet": "", "source": "bad"}]

    class Chatty(BadBackend):
        async def search(self, query: str, max_results: int) -> list[dict]:  # type: ignore[type-arg]
            return [{"title": "x", "url": f"https://a.example/{i}", "snippet": "", "source": "bad"} for i in range(5)]

    for backend in (BadBackend(), Chatty()):
        executor = ToolExecutor(ToolRegistry([WebSearchTool(backend)]), ToolPolicy())  # type: ignore[arg-type]
        result = await run(executor, "web_search", "researcher", query="q", max_results=2)
        assert result.error_type == "invalid_output"


# --- Python analysis -----------------------------------------------------------------------


async def test_fake_python_analysis_returns_scripted_output_without_executing() -> None:
    backend = FakeSandboxBackend({"print(sum([1, 2, 3]))": PythonAnalysisOutput(stdout="6\n", result=6, backend="fake-sandbox")})
    executor = ToolExecutor(ToolRegistry([PythonAnalysisTool(backend)]), ToolPolicy())

    ok_result = await run(executor, "python_analysis", code="print(sum([1, 2, 3]))")
    assert ok_result.success and ok_result.output["stdout"] == "6\n" and ok_result.metadata["fake"] is True

    marker = Path("nexus_fake_sandbox_marker.txt")
    evil = await run(executor, "python_analysis", code=f"open('{marker}', 'w').write('pwned')")
    assert not evil.success and not marker.exists()  # the code was never run
    assert "python_analysis" not in build_tool_registry(Settings(_env_file=None))  # type: ignore[call-arg]


FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
FORBIDDEN_MODULES = ("subprocess", "os", "multiprocessing", "importlib", "ctypes", "pty", "shutil")


def test_tool_code_has_no_code_execution_or_process_primitives() -> None:
    offenders = []
    for path in APP_TOOLS.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                offenders.append(f"{path.name}: {node.func.id}()")
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            offenders += [f"{path.name}: import {n}" for n in names if n.split(".")[0] in FORBIDDEN_MODULES]
    assert offenders == []


async def test_fake_calculator_is_scripted() -> None:
    executor = ToolExecutor(ToolRegistry([FakeCalculator({"2+2": 5})]), ToolPolicy())
    result = await run(executor, "calculator", expression="2+2")
    assert result.output["result"] == 5 and result.metadata["fake"] is True  # clearly not real math
    assert (await run(executor, "calculator", expression="1+1")).error_type == "execution_error"
