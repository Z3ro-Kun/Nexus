"""Deterministic Phase 9 end-to-end demo: ONE objective -> plan -> parallel research ->
conflicting facts -> resolution -> analysis -> approval-gated order -> verification ->
completion, through the HTTP API (POST /execute, approve, POST /execute again). Not part
of the test suite.

Usage (from backend/):  uv run python ../scripts/demo_orchestration.py

A temporary SQLite database; FakeLLMProvider scripts the planner and agents; prices come
from FakeSearchBackend (fake-search.invalid, marked fake); the order is a
FakeSideEffectTool that counts its executions. No network, no real LLM.
Exit code 0 only if every check passed.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402

import app.models  # noqa: E402,F401 - registers tables
from app.core.config import Settings, get_settings  # noqa: E402
from app.events.base import Event  # noqa: E402
from app.events.factory import parse_payload  # noqa: E402
from app.events.types import ActionCategory  # noqa: E402
from app.main import create_app  # noqa: E402
from app.persistence.database import Base, Database  # noqa: E402
from app.state.projector import project  # noqa: E402
from app.tools.fakes import FakeSideEffectTool  # noqa: E402
from tests.orchestration_fixtures import APPROVAL, GOAL, llm, plan, steps  # noqa: E402
from tests.recovery_fixtures import search_backend  # noqa: E402
from tests.tool_fixtures import fake_registry  # noqa: E402

results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}")


async def main() -> int:
    order = FakeSideEffectTool("place_order", ActionCategory.IRREVERSIBLE)
    registry = fake_registry(search_backend(fail=[]))
    registry.register(order)
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(f"sqlite+aiosqlite:///{Path(tmp) / 'demo.db'}")
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        settings = Settings(_env_file=None, NEXUS_ENVIRONMENT="test", NEXUS_MAX_TOOL_CALLS_PER_TASK=3,  # type: ignore[call-arg]
                            NEXUS_VERIFICATION_SEMANTIC=False)
        app = create_app(settings, database=db, llm_provider=llm(steps(94999, 99999), planner=plan(with_action=True)),
                         tool_registry=registry)
        app.dependency_overrides[get_settings] = lambda: settings
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo") as api:
            run_id = (await api.post("/api/v1/runs", json={"goal": GOAL})).json()["id"]
            base = f"/api/v1/runs/{run_id}"
            first = (await api.post(f"{base}/execute")).json()
            print(f"\n1st /execute -> phase={first['phase']} approvals_pending={first['approvals_pending']} orders={order.count}")
            check("pauses for approval before the side effect", first["phase"] == "waiting_for_approval" and order.count == 0)
            again = (await api.post(f"{base}/execute")).json()
            check("repeated call while waiting changes nothing", again["passes"] == 1 and again["started"] == [] and order.count == 0)
            await api.post(f"{base}/approvals/{APPROVAL}/approve", json={"actor": "demo-user", "reason": "within budget"})
            check("approving executes nothing by itself", order.count == 0)
            done = (await api.post(f"{base}/execute")).json()
            print(f"2nd /execute -> phase={done['phase']} orders={order.count}")
            result = (await api.get(f"{base}/result")).json()
            api_state = (await api.get(f"{base}/state")).json()
            raw = (await api.get(f"{base}/events")).json()
        await db.dispose()

    events = [Event.model_validate({**e, "payload": parse_payload(e["event_type"], e["payload"])}) for e in raw]
    print("\n== Event history (sequence, type, task, agent)")
    for e in events:
        print(f"  {e.sequence:>3}  {e.event_type.value:<20} {e.task_id or '-':<34} {e.agent_id or '-'}")
    print("\n== Final result")
    print(f"  objective: {result['objective']}")
    print(f"  phase={result['phase']} verified={result['verified']} summary={result['completion_summary']}")
    for d in result["deliverables"]:
        print(f"  deliverable {d['task_id']}: {d['summary']} artifacts={[a['name'] for a in d['artifacts']]}")
    for c in result["conflicts"]:
        print(f"  conflict {c['conflict_id']} {c['status']} on {c['fact_key']} -> accepted {c['accepted_fact_id']}")
    for a in result["approvals"]:
        print(f"  approval {a['approval_id']} {a['status']} by {a['actor']} executed={a['executed']}")
    print()
    check("executed the approved action exactly once", order.count == 1)
    check("conflict resolved by independent evidence", [c["status"] for c in result["conflicts"]] == ["resolved"])
    check("verified and completed", done["phase"] == "completed" and result["verified"])
    check("the recommendation is the deliverable", [d["task_id"] for d in result["deliverables"]] == ["compare"]
          and result["deliverables"][0]["artifacts"][0]["name"] == "recommendation")
    check("checkpoint created by NEXUS, not the planner", any(t["task_id"] == "verify.objective" and t["is_checkpoint"] for t in result["tasks"]))
    check("event reconstruction matches the API state", project(events).model_dump(mode="json") == api_state, f"{len(events)} events")
    print(f"\n{sum(results)}/{len(results)} checks passed. REAL LLM ORCHESTRATION: NOT VERIFIED (FakeLLMProvider).")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
