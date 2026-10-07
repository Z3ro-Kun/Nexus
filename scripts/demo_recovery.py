"""Deterministic Phase 5 end-to-end demo: failure -> classification -> replan ->
replacement -> execution -> event reconstruction. Not part of the test suite.

Usage (from backend/):  uv run python ../scripts/demo_recovery.py

Uses a temporary SQLite database, FakeLLMProvider (planner, agents AND replanner are
scripted) and FakeSearchBackend with an injected outage for "source A". No network, no
real LLM: this demonstrates NEXUS's recovery mechanics, not model quality.
Exit code 0 only if every check passed.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import app.models  # noqa: E402,F401 - registers tables
from app.events.types import EventType, ReplanTriggered  # noqa: E402
from app.persistence.database import Base, Database  # noqa: E402
from app.state.models import TaskStatus  # noqa: E402
from app.state.projector import project  # noqa: E402
from tests.recovery_fixtures import (  # noqa: E402
    A, B, C, COMPARE, QA, SOURCE_C_REPLAN, Harness, in_order, provider, replan, search_backend, timeline,
)

results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}")


async def fresh_db(directory: str, name: str) -> Database:
    db = Database(f"sqlite+aiosqlite:///{Path(directory) / name}")
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return db


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        # --- scenario 1: tool failure recovered ---------------------------------------------
        db = await fresh_db(tmp, "demo.db")
        h = Harness(db, provider(in_order(SOURCE_C_REPLAN)), search_backend(fail=[QA]), max_replans=2)
        run_id = await h.planned_run()
        report = await h.schedule(run_id)
        events = await h.events(run_id)

        print("\n== Event history (sequence, type, subject task, agent)")
        for e, (kind, subject) in zip(events, timeline(events)):
            extra = ""
            if e.event_type is EventType.TASK_FAILED:
                extra = f"  failure_type={e.payload.failure_type.value} error_type={e.payload.error_type}"  # type: ignore[attr-defined]
            if isinstance(e.payload, ReplanTriggered):
                extra = f"  replan #{e.payload.replan_number} -> {e.payload.new_task_ids}: {e.payload.strategy_summary}"
            print(f"  {e.sequence:>3}  {kind:<16} {subject or '-':<20} {e.agent_id or '-':<11}{extra}")
        print()

        state = project(events)
        check("classification", state.tasks[A].failure is not None and state.tasks[A].failure.failure_type.value == "TOOL_FAILURE",
              f"{A}: {state.tasks[A].failure.failure_type.value}/{state.tasks[A].failure.error_type}" if state.tasks[A].failure else "none")
        check("historical failure remains visible", state.tasks[A].status is TaskStatus.FAILED and state.tasks[A].replaced_by == C,
              f"{A} FAILED, replaced_by={state.tasks[A].replaced_by}")
        check("replacement executed", state.tasks[C].status is TaskStatus.COMPLETED, f"{C}: {state.tasks[C].summary}")
        check("dependent task continued", state.tasks[COMPARE].status is TaskStatus.COMPLETED,
              f"{COMPARE} ran on {B} + {C}: {state.tasks[COMPARE].summary}")
        seq = {(e.event_type, getattr(e.payload, 'task_id', None) or getattr(e.payload, 'failed_task_id', None)): e.sequence for e in events}
        order = [seq[(EventType.TASK_FAILED, A)], seq[(EventType.REPLAN_TRIGGERED, A)], seq[(EventType.TASK_CREATED, C)],
                 seq[(EventType.TASK_STARTED, C)], seq[(EventType.TASK_COMPLETED, C)], seq[(EventType.TASK_STARTED, COMPARE)]]
        check("event ordering", order == sorted(order) and [e.sequence for e in events] == list(range(1, len(events) + 1)),
              "TaskFailed(A) < ReplanTriggered < TaskCreated(C) < TaskStarted(C) < TaskCompleted(C) < TaskStarted(compare)")
        check("event reconstruction", state == await h.state(run_id), f"{len(events)} events replayed to identical state")
        check("run not declared complete", state.status.value == "created" and EventType.RUN_COMPLETED not in [e.event_type for e in events],
              f"run status {state.status.value}; verification comes in a later phase")
        await db.dispose()

        # --- scenario 2: replan budget ----------------------------------------------------------
        db = await fresh_db(tmp, "budget.db")
        bad = replan("empty")  # always rejected
        h = Harness(db, provider(in_order(bad)), search_backend(fail=[QA]), max_replans=2)
        run_id = await h.planned_run()
        report = await h.schedule(run_id)
        state = await h.state(run_id)
        check("replan budget enforced", report.run_status.value == "failed" and len(h.replanner_calls()) == 2
              and state.recovery.replan_attempts == 2, f"RunFailed: {state.failure_reason}")
        await db.dispose()

    print(f"\n{sum(results)}/{len(results)} checks passed. Real LLM replanning: NOT exercised (FakeLLMProvider).")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
