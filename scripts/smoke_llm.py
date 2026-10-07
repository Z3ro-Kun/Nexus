"""Real-LLM smoke test for the configured provider (e.g. NEXUS_LLM_PROVIDER=openai). Not
part of the test suite. It makes REAL, billed API calls.

Usage (from backend/, after filling in .env):
    uv run python ../scripts/smoke_llm.py --step ping   # 1 call: tiny schema (auth, base URL, JSON)
    uv run python ../scripts/smoke_llm.py --step plan   # 1 call: the real planner on a small goal
    uv run python ../scripts/smoke_llm.py --step run    # plan + schedule (agents, tools, events)
    uv run python ../scripts/smoke_llm.py --step execute --goal "..."   # Phase 9 POST /execute,
        # minimal: <= 2 tasks, 1 tool call per task, no semantic verification, no replans,
        # no conflict resolution, no LLM retries, and a hard cap on real LLM calls
    uv run python ../scripts/smoke_llm.py --step swarm --max-calls 30   # one run of an objective
        # with independent parts: <= 6 tasks, the configured tool budget, semantic verification,
        # 1 replan; reports the plan's independent tasks and how many ran at once

Configuration comes from .env / the environment exactly as for the API. The plan and run
steps use a temporary SQLite database, never the configured PostgreSQL database. Secrets
are never printed: only the provider, model and base-URL host are shown.
Exit code 0 only if every check passed.
"""

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402

import app.models  # noqa: E402,F401 - registers tables
from app.core.config import Settings, get_settings  # noqa: E402
from app.core.exceptions import LLMError  # noqa: E402
from app.events.base import Event  # noqa: E402
from app.events.factory import parse_payload  # noqa: E402
from app.llm.provider import build_provider  # noqa: E402
from app.llm.schemas import LLMMessage, LLMRequest  # noqa: E402
from app.main import create_app  # noqa: E402
from app.persistence.database import Base, Database  # noqa: E402
from app.state.projector import project  # noqa: E402

DEFAULT_GOAL = (
    "Compare binary search and linear search for finding an item in a sorted list of one "
    "million integers, and recommend one."
)
PING_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["ok"]}},
    "required": ["status"],
    "additionalProperties": False,
}

results: list[bool] = []


class CallBudget:
    """Wraps the configured provider (the real one) with a hard cap on calls, and records
    each call's purpose and token usage. Smoke-test harness only; not used by NEXUS."""

    def __init__(self, inner: object, max_calls: int) -> None:
        self._inner = inner
        self.name = getattr(inner, "name", "unknown")
        self.max_calls = max_calls
        self.calls: list[tuple[str, int | None, int | None]] = []
        # Who answered each call (task id, provider, model), and how many calls were in flight at once.
        self.answered: list[tuple[str, str, str, str]] = []
        self.active: set[int] = set()
        self.max_active = 0
        self.peak: set[str] = set()
        self._labels: dict[int, str] = {}

    async def generate(self, request: LLMRequest):  # type: ignore[no-untyped-def]
        if len(self.calls) >= self.max_calls:
            raise LLMError(f"smoke-test call budget of {self.max_calls} real LLM calls exhausted ({request.purpose})")
        index = len(self.calls)
        self.calls.append((request.purpose, None, None))
        self._labels[index] = request.metadata.get("task_id", request.purpose)
        self.active.add(index)
        if len(self.active) > self.max_active:
            self.max_active = len(self.active)
            self.peak = {self._labels[i] for i in self.active}
        try:
            response = await self._inner.generate(request)  # type: ignore[attr-defined]
        finally:
            self.active.discard(index)
        self.calls[index] = (request.purpose, response.usage.input_tokens, response.usage.output_tokens)
        self.answered.append((request.purpose, self._labels[index], response.provider, response.model))
        return response


def overlap(events: list[Event]) -> tuple[int, list[str]]:
    """The most tasks RUNNING at the same point of the event log, and which ones."""
    running: set[str] = set()
    best: list[str] = []
    for e in events:
        kind = e.event_type.value
        if kind == "TaskStarted" and e.task_id:
            running.add(e.task_id)
            if len(running) > len(best):
                best = sorted(running)
        elif kind in ("TaskCompleted", "TaskFailed") and e.task_id:
            running.discard(e.task_id)
    return len(best), best


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}")


def describe(settings: Settings) -> None:
    host = urlsplit(settings.openai_base_url).hostname if settings.openai_base_url else None
    print(f"provider={settings.llm_provider} model={settings.llm_model}", end="")
    if settings.llm_provider == "openai":
        print(
            f" base_url_host={host or 'api.openai.com (default)'}"
            f" response_format={settings.openai_response_format} strict={settings.openai_strict_schema}"
            f" key={'set' if settings.openai_api_key else 'NOT SET'}",
            end="",
        )
    print(f" max_tokens={settings.llm_max_tokens}")


async def ping(settings: Settings) -> None:
    provider = build_provider(settings)
    assert provider is not None
    try:
        response = await provider.generate(
            LLMRequest(
                purpose="smoke:ping",
                system="You are a connectivity check. Reply with the JSON object requested.",
                messages=[LLMMessage(role="user", content='Reply with {"status": "ok"}.')],
                output_schema=PING_SCHEMA,
                # Full configured cap: reasoning models spend output tokens before the JSON.
                max_tokens=settings.llm_max_tokens,
            )
        )
    except LLMError as exc:
        check("ping", False, f"{type(exc).__name__}: {exc}")
        return
    check("ping", response.data == {"status": "ok"},
          f"data={response.data} model={response.model} tokens={response.usage.input_tokens}/{response.usage.output_tokens}")


async def plan_or_run(settings: Settings, goal: str, schedule: bool) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(f"sqlite+aiosqlite:///{Path(tmp) / 'smoke.db'}")
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        app = create_app(settings, database=db)
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://smoke", timeout=None
            ) as api:
                run_id = (await api.post("/api/v1/runs", json={"goal": goal})).json()["id"]
                base = f"/api/v1/runs/{run_id}"
                planned = await api.post(f"{base}/plan")
                body = planned.json()
                if planned.status_code != 201:
                    check("plan", False, f"HTTP {planned.status_code}: {body}")
                    return
                check("plan", True, f"{len(body['tasks'])} tasks from {body['provider']}/{body['model']}")
                for task in body["tasks"]:
                    print(f"      {task['task_id']:<28} {task['agent_type']:<11} deps={task.get('dependencies', [])}")
                if not schedule:
                    return

                scheduled = await api.post(f"{base}/schedule")
                report = scheduled.json()
                if scheduled.status_code != 200:
                    check("schedule", False, f"HTTP {scheduled.status_code}: {report}")
                    return
                statuses = report["task_statuses"]
                check("schedule", all(s == "completed" for s in statuses.values()),
                      f"run={report['run_status']} tasks={statuses}")
                api_state = (await api.get(f"{base}/state")).json()
                raw = (await api.get(f"{base}/events")).json()
        finally:
            await db.dispose()

    events = [Event.model_validate({**e, "payload": parse_payload(e["event_type"], e["payload"])}) for e in raw]
    print("\n== Event history (sequence, type, subject, agent)")
    for e in events:
        p = e.payload
        subject = getattr(p, "fact_id", None) or getattr(p, "task_id", None) or getattr(p, "conflict_id", None) or "-"
        print(f"  {e.sequence:>3}  {e.event_type.value:<18} {subject:<36} {e.agent_id or '-'}")
    state = project(events)
    for task_id, task in state.tasks.items():
        print(f"\n[{task_id}] {task.status.value}: {task.summary or task.error or ''}")
    claims = sum(1 for f in state.facts.values() if f.claim is not None)
    print(f"\nfacts={len(state.facts)} (with claim: {claims}) conflicts={len(state.conflicts)} "
          f"replans={state.recovery.replan_attempts}")
    check("event reconstruction", state.model_dump(mode="json") == api_state, f"{len(events)} events")


# --step execute: the cheapest possible run. It allows at most 2 tasks and 1 tool call per
# task, so it cannot show parallel decomposition (one task must combine the other's result).
MINIMAL = {
    "max_planned_tasks": 2, "verification_semantic": False, "max_replans_per_run": 0,
    "max_conflict_resolutions_per_run": 0, "max_tool_calls_per_task": 1, "max_orchestration_passes": 3,
    "llm_max_retries": 0,
}
# --step swarm: one scoped run that leaves room for genuine parallel decomposition. The
# tool budget stays as configured (.env), so splitting is never forced by a tool limit.
SWARM = {
    "max_planned_tasks": 6, "verification_semantic": True, "max_replans_per_run": 1,
    "max_conflict_resolutions_per_run": 1, "max_orchestration_passes": 4,
}
SWARM_GOAL = (
    "Fetch each of these four pages and report its HTML page title: https://example.com, "
    "https://example.org, https://example.net and https://www.iana.org/help/example-domains. "
    "Then compare the four titles and state which of them are identical."
)


async def execute(settings: Settings, goal: str, max_calls: int, profile: dict[str, object] = MINIMAL) -> None:
    """One Phase 9 run through the API (POST /runs, POST /execute), on a temporary SQLite
    database, with the given configuration profile (see MINIMAL and SWARM)."""
    settings = settings.model_copy(update=profile)
    inner = build_provider(settings)
    assert inner is not None
    budget = CallBudget(inner, max_calls)
    print(f"run profile: max_planned_tasks={settings.max_planned_tasks} max_tool_calls_per_task={settings.max_tool_calls_per_task} "
          f"semantic={settings.verification_semantic} replans={settings.max_replans_per_run} "
          f"conflict_resolutions={settings.max_conflict_resolutions_per_run} llm_retries={settings.llm_max_retries} "
          f"http_fetch={settings.http_fetch_enabled} call_cap={max_calls}")
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(f"sqlite+aiosqlite:///{Path(tmp) / 'smoke.db'}")
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        app = create_app(settings, database=db, llm_provider=budget)  # type: ignore[arg-type]
        app.dependency_overrides[get_settings] = lambda: settings
        print(f"tools registered: {list(app.state.tool_registry.names)}")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://smoke", timeout=None) as api:
                run_id = (await api.post("/api/v1/runs", json={"goal": goal})).json()["id"]
                base = f"/api/v1/runs/{run_id}"
                response = await api.post(f"{base}/execute")
                outcome = response.json()
                api_state = (await api.get(f"{base}/state")).json()
                raw = (await api.get(f"{base}/events")).json()
        finally:
            await db.dispose()

    print(f"\n== Real LLM calls: {len(budget.calls)}")
    for purpose, tokens_in, tokens_out in budget.calls:
        print(f"  {purpose:<18} input={tokens_in} output={tokens_out}")
    total_in = sum(t or 0 for _, t, _ in budget.calls)
    total_out = sum(t or 0 for _, _, t in budget.calls)
    print(f"  total tokens: input={total_in} output={total_out} sum={total_in + total_out}")
    print("\n== Who answered (purpose, task, provider, model)")
    for purpose, label, provider_name, model in budget.answered:
        print(f"  {purpose:<18} {label:<24} {provider_name:<11} {model}")
    print(f"  most real LLM calls in flight at once: {budget.max_active} {sorted(budget.peak)}")
    if response.status_code != 200:
        check("execute", False, f"HTTP {response.status_code}: {outcome}")
        return
    events = [Event.model_validate({**e, "payload": parse_payload(e["event_type"], e["payload"])}) for e in raw]
    state = project(events)
    print("\n== Event history (sequence, type, task, agent)")
    for e in events:
        print(f"  {e.sequence:>3}  {e.event_type.value:<20} {e.task_id or '-':<24} {e.agent_id or '-'}")
    print("\n== Tasks")
    for t in state.tasks.values():
        print(f"  {t.task_id:<20} {t.agent_type or '-':<16} {t.status.value:<10} deps={list(t.dependencies)}  {t.summary or t.error or ''}"[:400])
    print("\n== Tool calls")
    for c in state.tool_calls.values():
        out = c.output if isinstance(c.output, dict) else {}
        print(f"  {c.tool_call_id} {c.tool_name} {c.status.value} args={c.arguments} "
              f"status_code={out.get('status_code')} final_url={out.get('final_url')} fake={c.metadata.get('fake')} {c.error_type or ''}")
    print("\n== Facts")
    for f in state.facts.values():
        pv = f.provenance
        print(f"  {f.fact_id}: {f.content[:160]!r}")
        print(f"      provenance kind={pv.kind if pv else None} tool={pv.tool_name if pv else None} "
              f"call={pv.tool_call_id if pv else None} source={pv.source if pv else None} fake={pv.fake if pv else None}")
    for a in state.artifacts.values():
        print(f"  artifact {a.artifact_id} {a.name} ({a.media_type}): {a.content[:200]!r}")
    print("\n== Verification")
    for v in state.verifications.values():
        print(f"  {v.verification_id} attempt {v.attempt}: {v.status.value} semantic={'yes' if v.semantic else 'no'}")
        for c in v.checks:
            print(f"    {'PASS' if c.passed else 'FAIL'} {c.check_id}: {c.message[:160]}")
    print(f"\n== Outcome: phase={outcome['phase']} run_status={state.status.value} passes={outcome['passes']} "
          f"replanned={outcome['replanned']} conflicts={len(state.conflicts)} approvals={len(state.approvals)} "
          f"policy_decisions={len(state.policy)}")
    print(f"   blockers={outcome['result']['completion_blockers']}")
    peak, tasks = overlap(events)
    roots = [t.task_id for t in state.tasks.values() if not t.dependencies and t.verification is None]
    print(f"\n== Parallelism: {len(state.tasks)} tasks, {len(roots)} without dependencies {roots}; "
          f"most RUNNING at once (event log): {peak} {tasks}")
    check("run completed", outcome["phase"] == "completed")
    check("event reconstruction matches API state", state.model_dump(mode="json") == api_state, f"{len(events)} events")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--step", choices=["ping", "plan", "run", "execute", "swarm"], required=True)
    parser.add_argument("--max-calls", type=int, default=6, help="execute: hard cap on real LLM calls")
    parser.add_argument("--goal", default=DEFAULT_GOAL)
    parser.add_argument("--max-tasks", type=int, default=3, help="planner task limit (default 3, keeps cost low)")
    args = parser.parse_args()

    settings = Settings()
    if args.step != "swarm":
        settings = settings.model_copy(update={"max_planned_tasks": min(args.max_tasks, settings.max_planned_tasks)})
    describe(settings)
    if build_provider(settings) is None:
        print("FAIL  no LLM provider configured (set NEXUS_LLM_PROVIDER in .env)")
        return 1

    if args.step == "ping":
        await ping(settings)
    elif args.step == "swarm":
        await execute(settings, args.goal if args.goal != DEFAULT_GOAL else SWARM_GOAL, args.max_calls, SWARM)
    elif args.step == "execute":
        await execute(settings, args.goal, args.max_calls)
    else:
        await plan_or_run(settings, args.goal, schedule=args.step == "run")
    print(f"\n{sum(results)}/{len(results)} checks passed.")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
