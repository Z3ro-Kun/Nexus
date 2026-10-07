"""Opt-in verification of REAL tool behavior (not part of the test suite).

Usage (from backend/):  uv run python ../scripts/verify_real_tools.py

- calculator: real implementation (offline).
- http_fetch: REAL network: fetches https://example.com through the SSRF guard (real DNS,
  IP pinning, TLS with SNI), then checks that real SSRF targets are refused, including
  a public hostname that resolves to 127.0.0.1 (localtest.me).
- web_search / python_analysis: no real backend exists; reported as not verifiable.
Exit code 0 only if every executed check passed.
"""

import asyncio
import sys
from uuid import uuid4

from app.tools.calculator import CalculatorTool
from app.tools.executor import ToolExecutor
from app.tools.http_fetch import HTTPFetchTool
from app.tools.policy import ToolPolicy
from app.tools.registry import ToolRegistry
from app.tools.schemas import ToolCall, ToolContext

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


async def main() -> int:
    executor = ToolExecutor(ToolRegistry([CalculatorTool(), HTTPFetchTool()]), ToolPolicy.with_specialist_tools(["calculator", "http_fetch"]))

    async def call(tool: str, **args: object):  # type: ignore[no-untyped-def]
        context = ToolContext(run_id=uuid4(), task_id="verify", agent_type="specialist", tool_call_id="verify.t1")
        return await executor.execute(ToolCall(tool_name=tool, arguments=args), context)  # type: ignore[arg-type]

    r = await call("calculator", expression="(1299 - 999) / 999 * 100")
    check("calculator arithmetic", r.success and abs(r.output["result"] - 30.03003) < 1e-4, str(r.output))
    r = await call("calculator", expression="__import__('os').system('echo pwned')")
    check("calculator rejects code", r.error_type == "invalid_arguments", str(r.error))

    r = await call("http_fetch", url="https://example.com/")
    ok = r.success and r.output["status_code"] == 200 and "Example Domain" in (r.output["content"] or "")
    detail = f"status={r.output['status_code']} bytes={r.output['content_bytes']} type={r.output['content_type']}" if r.success else f"{r.error_type}: {r.error}"
    check("http_fetch real HTTPS page (DNS + IP pinning + TLS/SNI)", ok, detail)

    for url in ["http://127.0.0.1/", "http://localhost/", "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/", "http://localtest.me/", "http://[::1]/"]:
        r = await call("http_fetch", url=url)
        check(f"http_fetch refuses {url}", r.error_type == "ssrf_blocked", f"{r.error_type}: {r.error}")

    print("NOT VERIFIABLE  web_search: no real search backend is implemented")
    print("NOT VERIFIABLE  python_analysis: no sandbox backend is available")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
