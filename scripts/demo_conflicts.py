"""Deterministic Phase 6 end-to-end demo: conflicting facts -> ConflictDetected ->
resolution task -> new evidence -> ConflictResolved, through the HTTP API. Not part of the
test suite.

Usage (from backend/):  uv run python ../scripts/demo_conflicts.py

A temporary SQLite database; FakeLLMProvider scripts the planner and agents; every price
comes from FakeSearchBackend (fake-search.invalid, marked fake). No network, no real LLM.
Exit code 0 only if every check passed.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402

import app.models  # noqa: E402,F401 - registers tables
from app.core.config import Settings  # noqa: E402
from app.events.base import Event  # noqa: E402
from app.events.factory import parse_payload  # noqa: E402
from app.main import create_app  # noqa: E402
from app.persistence.database import Base, Database  # noqa: E402
from app.state.projector import project  # noqa: E402
from tests.agent_fixtures import GOAL  # noqa: E402
from tests.conflict_fixtures import QA, QB, QC, fake_search, provider  # noqa: E402
from tests.recovery_fixtures import url  # noqa: E402
from tests.tool_fixtures import fake_registry  # noqa: E402

results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}")


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(f"sqlite+aiosqlite:///{Path(tmp) / 'demo.db'}")
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        settings = Settings(_env_file=None, NEXUS_ENVIRONMENT="test")  # type: ignore[call-arg]
        app = create_app(settings, database=db, llm_provider=provider(), tool_registry=fake_registry(fake_search()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo") as api:
            run_id = (await api.post("/api/v1/runs", json={"goal": GOAL})).json()["id"]
            base = f"/api/v1/runs/{run_id}"
            await api.post(f"{base}/plan")
            report = (await api.post(f"{base}/schedule")).json()
            api_state = (await api.get(f"{base}/state")).json()
            raw = (await api.get(f"{base}/events")).json()
        await db.dispose()

    events = [Event.model_validate({**e, "payload": parse_payload(e["event_type"], e["payload"])}) for e in raw]
    print("\n== Event history (sequence, type, subject, agent)")
    for e in events:
        p = e.payload
        subject = getattr(p, "fact_id", None) or getattr(p, "task_id", None) or getattr(p, "conflict_id", None) or getattr(p, "tool_call_id", None) or "-"
        extra = ""
        if getattr(p, "claim", None):
            extra = f"  {p.claim.subject} / {p.claim.attribute} = {p.claim.value} {p.claim.unit}  [{p.provenance.source}, fake={p.provenance.fake}]"  # type: ignore[union-attr]
        elif e.event_type.value == "ConflictDetected":
            extra = f"  {p.conflict_type.value}: {p.reason}"  # type: ignore[attr-defined]
        elif e.event_type.value == "ConflictResolved":
            extra = f"  accepted={p.resolved_fact_id} evidence={p.evidence_fact_ids}: {p.reason}"  # type: ignore[attr-defined]
        print(f"  {e.sequence:>3}  {e.event_type.value:<17} {subject:<32} {e.agent_id or '-':<18}{extra}")
    print()

    state = project(events)
    [cid] = report["conflicts_detected"]
    c = state.conflicts[cid]
    fa, fb, evidence = "research_a.f1", "research_b.f1", c.resolved_fact_id
    check("original facts remain", {fa, fb} <= set(state.facts) and (state.facts[fa].claim.value, state.facts[fb].claim.value) == (94999, 99999),  # type: ignore[union-attr]
          f"{fa}=94999 INR, {fb}=99999 INR")
    check("conflict remains represented", c.fact_ids == (fa, fb) and c.conflict_type is not None,
          f"{cid}: {c.conflict_type.value if c.conflict_type else '?'} on {c.fact_key}")
    check("conflict resolved", c.status.value == "resolved" and report["conflicts_resolved"] == [cid], f"status={c.status.value}")
    ev = state.facts.get(evidence or "")
    check("resolution evidence recorded", ev is not None and ev.claim is not None and ev.claim.value == 96999 and ev.task_id == c.resolver_task_id,
          f"{evidence} = 96999 INR from task {c.resolver_task_id}")
    prov_ok = all(
        state.facts[f].provenance is not None and state.facts[f].provenance.kind == "tool_output"  # type: ignore[union-attr]
        and state.facts[f].provenance.source == url(q) and state.facts[f].provenance.fake  # type: ignore[union-attr]
        for f, q in ((fa, QA), (fb, QB), (evidence, QC))
    )
    check("provenance intact", prov_ok, "all three prices tool-derived (web_search), distinct fake sources, fake=True")
    kinds = [e.event_type.value for e in events]
    order = [kinds.index("ConflictDetected"), max(i for i, k in enumerate(kinds) if k == "FactAdded" and events[i].task_id in ("research_a", "research_b"))]
    seq_ok = (order[1] < order[0] < kinds.index("ConflictResolved")
              and [e.sequence for e in events] == list(range(1, len(events) + 1)))
    check("event ordering", seq_ok, "FactAdded(A,B) < ConflictDetected < TaskCreated(resolve) < ... < ConflictResolved")
    check("reconstruction matches API state", state.model_dump(mode="json") == api_state, f"{len(events)} events")
    print(f"\n{sum(results)}/{len(results)} checks passed. REAL LLM CONFLICT RESOLUTION: NOT VERIFIED (FakeLLMProvider).")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
