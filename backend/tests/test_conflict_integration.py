"""Conflicting facts -> ConflictDetected -> resolution task -> evidence -> verdict, end to
end over the event store (SQLite and PostgreSQL), including Phase 5 recovery of a failed
resolution task and concurrent resolution of independent conflicts.

LLM: FakeLLMProvider. Tools: FakeSearchBackend (fake, deterministic). No network.
"""

import asyncio
from typing import Any

import httpx

from app.core.config import Settings
from app.events.types import ConflictResolved, ConflictType, EventType, FailureType
from app.events.base import Event
from app.events.factory import parse_payload
from app.main import create_app
from app.persistence.database import Database
from app.state.conflict_detector import conflict_fingerprint, conflict_id_for, current_value
from app.state.models import ConflictStatus, TaskStatus
from app.state.projector import apply, project
from app.conflicts.resolution import resolution_task_id
from tests.agent_fixtures import GOAL
from tests.conflict_fixtures import (
    COMPARE,
    PLAN,
    QA,
    QB,
    QC,
    QD,
    RA,
    RB,
    STEPS,
    ConflictHarness,
    claimed_fact,
    fake_search,
    provider,
    replacement_replanner,
    resolver_finds,
    search_then_report,
)
from tests.recovery_fixtures import url
from tests.tool_fixtures import fake_registry

S = TaskStatus
FA, FB = f"{RA}.f1", f"{RB}.f1"
CONFLICT = conflict_id_for(conflict_fingerprint(ConflictType.NUMERIC_DISAGREEMENT, [FA, FB]))
RESOLVER = resolution_task_id(CONFLICT)


def subject_of(event: Any) -> str | None:
    p = event.payload
    return getattr(p, "task_id", None) or getattr(p, "conflict_id", None) or event.task_id


# --- 11-13, 19-21, demo sequence ------------------------------------------------------------------


async def test_conflict_is_detected_resolved_with_new_evidence_and_history_kept(database: Database) -> None:
    h = ConflictHarness(database, provider(), fake_search())
    run_id = await h.planned_run()

    report = await h.scheduler.run(run_id)

    assert report.conflicts_detected == [CONFLICT] and report.conflicts_resolved == [CONFLICT]
    assert report.task_statuses == {RA: S.COMPLETED, RB: S.COMPLETED, COMPARE: S.COMPLETED, RESOLVER: S.COMPLETED}

    events = await h.events(run_id)
    # FactAdded is labelled with its task (envelope); the two researchers may finish in either order.
    story = [(e.event_type.value, subject_of(e)) for e in events
             if e.event_type in (EventType.FACT_ADDED, EventType.CONFLICT_DETECTED, EventType.CONFLICT_RESOLVED)
             or subject_of(e) == RESOLVER]
    assert sorted(story[:2]) == [("FactAdded", RA), ("FactAdded", RB)]
    assert story[2:] == [
        ("ConflictDetected", CONFLICT),
        ("TaskCreated", RESOLVER),
        ("TaskStarted", RESOLVER),
        ("ToolCalled", RESOLVER),
        ("ToolSucceeded", RESOLVER),
        ("FactAdded", RESOLVER),
        ("TaskCompleted", RESOLVER),
        ("ConflictResolved", CONFLICT),
    ]
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))

    state = project(events)
    c = state.conflicts[CONFLICT]
    evidence = f"{RESOLVER}.f1"
    assert (c.status, c.fact_ids, c.resolution_task_id, c.resolver_task_id) == (ConflictStatus.RESOLVED, (FA, FB), RESOLVER, RESOLVER)
    assert (c.resolved_fact_id, c.evidence_ids, c.corroborated_fact_ids) == (evidence, (evidence,), ())
    # Original facts remain, with their own provenance (tool-derived, fake).
    for fid, query in ((FA, QA), (FB, QB), (evidence, QC)):
        p = state.facts[fid].provenance
        assert p is not None and (p.kind, p.tool_name, p.source, p.fake) == ("tool_output", "web_search", url(query), True)
    assert (state.facts[FA].claim.value, state.facts[FB].claim.value, state.facts[evidence].claim.value) == (94999, 99999, 96999)  # type: ignore[union-attr]
    assert state.facts[evidence].task_id == RESOLVER and state.facts[evidence].agent_id == "researcher"
    assert current_value(state, "Product X", "price").fact_ids == (evidence,)

    [resolved] = [e for e in events if isinstance(e.payload, ConflictResolved)]
    assert (resolved.agent_id, resolved.task_id) == ("conflict_resolver", RESOLVER)

    # 12: the resolution agent was given the conflict, facts, provenance and objective.
    request = next(r for r in h.llm.requests if r.metadata.get("task_id") == RESOLVER)
    content = request.messages[0].content
    for expected in ('"conflict_type": "NUMERIC_DISAGREEMENT"', f'"fact_id": "{FA}"', url(QA), url(QB), f'"task_id": "{RA}"', "Determine the current verified price"):
        assert expected in content, expected
    # Ordinary tasks do not see a conflict section.
    compare_request = next(r for r in h.llm.requests if r.metadata.get("task_id") == COMPARE)
    assert '"conflict"' not in compare_request.messages[0].content

    # 25: raw events reconstruct the service state.
    incremental = None
    for event in events:
        incremental = apply(incremental, event)
    assert state == incremental == await h.state(run_id)


# --- 14: no reliable evidence -> UNRESOLVED -------------------------------------------------------


async def test_resolution_with_only_a_disputed_source_is_unresolved(database: Database) -> None:
    h = ConflictHarness(database, provider(resolvers=resolver_finds(QA, 94999)), fake_search())
    run_id = await h.planned_run()

    report = await h.scheduler.run(run_id)

    assert report.conflicts_unresolved == [CONFLICT] and report.conflicts_resolved == []
    state = await h.state(run_id)
    c = state.conflicts[CONFLICT]
    assert c.status is ConflictStatus.UNRESOLVED and c.resolved_fact_id is None
    assert c.evidence_ids == (f"{RESOLVER}.f1",) and "same source as a conflicting fact" in (c.resolution or "")
    assert {FA, FB, f"{RESOLVER}.f1"} <= set(state.facts)  # all evidence preserved
    assert current_value(state, "Product X", "price").status == "conflicting"


# --- 15 / 22: resolution task fails -> normal failure; conflict stays OPEN ------------------------


async def test_failed_resolution_task_leaves_the_conflict_open(database: Database) -> None:
    h = ConflictHarness(database, provider(), fake_search(fail=[QC]), recovery=False)
    run_id = await h.planned_run()

    report = await h.scheduler.run(run_id)

    assert report.task_statuses[RESOLVER] is S.FAILED and report.conflicts_resolved == []
    state = await h.state(run_id)
    assert state.conflicts[CONFLICT].status is ConflictStatus.OPEN
    failure = state.tasks[RESOLVER].failure
    assert failure is not None and (failure.failure_type, failure.error_type) == (FailureType.TOOL_FAILURE, "unavailable")
    assert [e.event_type for e in await h.events(run_id)].count(EventType.TASK_FAILED) == 1


# --- 22-24: Phase 5 recovery replaces the failed resolution task, which then resolves -------------


async def test_replacement_of_a_failed_resolution_task_resolves_the_conflict(database: Database) -> None:
    retry = "resolve_price_via_d"
    llm = provider(
        resolvers={"resolve_conflict_": resolver_finds(QC, 96999), retry: resolver_finds(QD, 96999)},
        replanner=replacement_replanner(retry, QD),
    )
    h = ConflictHarness(database, llm, fake_search(fail=[QC]), recovery=True)
    run_id = await h.planned_run()

    report = await h.scheduler.run(run_id)

    assert report.replanned == [retry] and report.conflicts_resolved == [CONFLICT]
    state = await h.state(run_id)
    assert state.tasks[RESOLVER].status is S.FAILED and state.tasks[RESOLVER].replaced_by == retry
    assert state.tasks[retry].replaces == RESOLVER and state.tasks[retry].conflict_id is None  # linked via replacement
    c = state.conflicts[CONFLICT]
    assert (c.status, c.resolution_task_id, c.resolver_task_id, c.resolved_fact_id) == (
        ConflictStatus.RESOLVED, RESOLVER, retry, f"{retry}.f1",
    )
    [replan] = state.recovery.history
    assert (replan.failed_task_id, replan.failure_type) == (RESOLVER, FailureType.TOOL_FAILURE)
    # The replacement saw the conflict too (inherited through `replaces`).
    request = next(r for r in h.llm.requests if r.metadata.get("task_id") == retry)
    assert '"conflict_type": "NUMERIC_DISAGREEMENT"' in request.messages[0].content
    events = await h.events(run_id)
    order = [e.event_type.value for e in events if subject_of(e) in (RESOLVER, retry, CONFLICT)]
    assert order.index("TaskFailed") < order.index("ReplanTriggered") < order.index("ConflictResolved")
    assert project(events) == state


# --- 16-18: independent conflicts resolve concurrently ----------------------------------------------


async def test_independent_conflicts_are_resolved_concurrently(database: Database) -> None:
    qy_c = "product y price source c"
    steps = {
        RA: finish_two(RA, QA, 94999, 1000),
        RB: finish_two(RB, QB, 99999, 1200),
        COMPARE: STEPS[COMPARE],
    }
    fy_a, fy_b = f"{RA}.f2", f"{RB}.f2"
    cx = CONFLICT
    cy = conflict_id_for(conflict_fingerprint(ConflictType.NUMERIC_DISAGREEMENT, [fy_a, fy_b]))
    resolvers = {
        resolution_task_id(cx): resolver_finds(QC, 96999),
        resolution_task_id(cy): resolver_finds(qy_c, 1100, subject="Product Y"),
    }
    gates = {QC: asyncio.Event(), qy_c: asyncio.Event()}
    h = ConflictHarness(database, provider(steps, resolvers), fake_search(gates=gates))
    run_id = await h.planned_run()

    scheduling = asyncio.create_task(h.scheduler.run(run_id))
    await asyncio.wait_for(h.search.wait_until_searched(QC), 10)
    await asyncio.wait_for(h.search.wait_until_searched(qy_c), 10)
    assert h.search.active == {QC, qy_c}  # both resolution tasks in flight at once
    for gate in gates.values():
        gate.set()
    report = await asyncio.wait_for(scheduling, 10)

    assert sorted(report.conflicts_detected) == sorted([cx, cy]) and sorted(report.conflicts_resolved) == sorted([cx, cy])
    assert h.search.max_active >= 2
    events = await h.events(run_id)
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    state = project(events)
    assert state == await h.state(run_id)
    assert {c.conflict_id: c.resolution_task_id for c in state.conflicts.values()} == {cx: resolution_task_id(cx), cy: resolution_task_id(cy)}
    assert current_value(state, "Product Y", "price").value.value == 1100  # type: ignore[union-attr]
    for cid in (cx, cy):
        detected = next(e.sequence for e in events if e.event_type is EventType.CONFLICT_DETECTED and e.payload.conflict_id == cid)  # type: ignore[attr-defined]
        resolved = next(e.sequence for e in events if e.event_type is EventType.CONFLICT_RESOLVED and e.payload.conflict_id == cid)  # type: ignore[attr-defined]
        task_done = next(e.sequence for e in events if e.event_type is EventType.TASK_COMPLETED and e.payload.task_id == resolution_task_id(cid))  # type: ignore[attr-defined]
        assert detected < task_done < resolved


def finish_two(task_id: str, query: str, x: int, y: int) -> list[dict[str, Any]]:
    """One search, two claimed facts: Product X and Product Y prices."""
    return search_then_report(task_id, query, claimed_fact(task_id, query, x), claimed_fact(task_id, query, y, subject="Product Y"))


# --- corroboration end to end ---------------------------------------------------------------------


async def test_agreeing_sources_are_corroboration_without_conflict(database: Database) -> None:
    steps = {**STEPS, RB: search_then_report(RB, QB, claimed_fact(RB, QB, 94999))}
    h = ConflictHarness(database, provider(steps), fake_search())
    run_id = await h.planned_run()

    report = await h.scheduler.run(run_id)

    assert report.conflicts_detected == [] and RESOLVER not in report.task_statuses
    state = await h.state(run_id)
    value = current_value(state, "Product X", "price")
    assert (value.status, value.fact_ids) == ("corroborated", (FA, FB))


async def test_resolution_budget_zero_records_conflict_without_task(database: Database) -> None:
    h = ConflictHarness(database, provider(), fake_search(), max_resolutions=0)
    run_id = await h.planned_run()

    report = await h.scheduler.run(run_id)

    assert report.conflicts_detected == [CONFLICT] and RESOLVER not in report.task_statuses
    state = await h.state(run_id)
    assert state.conflicts[CONFLICT].status is ConflictStatus.OPEN and state.conflicts[CONFLICT].resolution_task_id is None


# --- API: reconstruction matches API state --------------------------------------------------------


async def test_api_schedule_detects_and_resolves_conflicts(settings: Settings, database: Database) -> None:
    app = create_app(settings, database=database, llm_provider=provider(), tool_registry=fake_registry(fake_search()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        run_id = (await api.post("/api/v1/runs", json={"goal": GOAL})).json()["id"]
        base = f"/api/v1/runs/{run_id}"
        assert (await api.post(f"{base}/plan")).status_code == 201
        body = (await api.post(f"{base}/schedule")).json()
        assert body["conflicts_detected"] == [CONFLICT] and body["conflicts_resolved"] == [CONFLICT]

        api_state = (await api.get(f"{base}/state")).json()
        raw = (await api.get(f"{base}/events")).json()
        rebuilt = project(
            [Event.model_validate({**e, "payload": parse_payload(e["event_type"], e["payload"])}) for e in raw]
        )
        assert rebuilt.model_dump(mode="json") == api_state
        conflict = api_state["conflicts"][CONFLICT]
        assert conflict["status"] == "resolved" and conflict["fact_ids"] == [FA, FB]
        assert api_state["facts"][FA]["claim"]["value"] == 94999

        body = (await api.post(f"{base}/schedule", json={"conflicts": False})).json()
        assert body["conflicts_detected"] == []


def test_plan_fixture_matches_the_demo_scenario() -> None:
    assert [t["id"] for t in PLAN["tasks"]] == [RA, RB, COMPARE]
