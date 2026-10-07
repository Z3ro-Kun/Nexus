"""Failure -> classification -> replan -> replacement -> execution -> reconstruction,
end to end over the event store, on SQLite and PostgreSQL.

LLM: FakeLLMProvider (planner, agents and replanner scripted). Tools: FakeSearchBackend
with deterministic injected failures, and the real offline calculator. No network.
"""

import asyncio
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import LLMError, LLMRateLimitError
from app.llm.fake import FakeLLMProvider, FakeReply
from app.llm.schemas import LLMRequest
from app.events.types import EventType, FailureType, ReplanTriggered
from app.main import create_app
from app.models.run import RunStatus
from app.persistence.database import Database
from app.state.models import TaskStatus
from app.state.projector import apply, project
from tests.agent_fixtures import GOAL
from tests.recovery_fixtures import (
    A,
    B,
    C,
    COMPARE,
    PLAN,
    QA,
    QB,
    QC,
    SOURCE_C_REPLAN,
    STEPS,
    Harness,
    agent_steps,
    in_order,
    provider,
    replan,
    replan_task,
    researcher_steps,
    search_backend,
    timeline,
)
from tests.tool_fixtures import fake_registry

S = TaskStatus
F = FailureType


def seq_of(events: list[Any], event_type: EventType, task_id: str) -> int:
    [match] = [
        e.sequence
        for e in events
        if e.event_type is event_type
        and (getattr(e.payload, "task_id", None) or getattr(e.payload, "failed_task_id", None)) == task_id
    ]
    return match


# --- 22 / demo: tool failure -> replan -> alternate source -> success ------------------------


async def test_tool_failure_is_recovered_with_an_alternate_source(database: Database) -> None:
    h = Harness(database, provider(in_order(SOURCE_C_REPLAN)), search_backend(fail=[QA]))
    run_id = await h.planned_run()
    events_before = await h.events(run_id)

    report = await h.schedule(run_id)

    # The original task stays FAILED; the replacement completed; the dependent ran.
    assert report.task_statuses == {A: S.FAILED, B: S.COMPLETED, COMPARE: S.COMPLETED, C: S.COMPLETED}
    assert report.replanned == [C] and report.failed == [A]
    assert report.run_status is RunStatus.CREATED  # never declared complete by execution alone
    assert h.search.queries.count(QA) == 1  # the failed action was not repeated
    assert QC in h.search.queries

    events = await h.events(run_id)
    # Historical events are untouched: the log only grew.
    assert [e.model_dump() for e in events[: len(events_before)]] == [e.model_dump() for e in events_before]
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))

    story = [x for x in timeline(events) if x[1] in (A, C, COMPARE) or x[0] == "ReplanTriggered"]
    assert story == [
        ("TaskCreated", A),
        ("TaskCreated", COMPARE),
        ("TaskStarted", A),
        ("ToolCalled", A),
        ("ToolFailed", A),
        ("TaskFailed", A),
        ("ReplanTriggered", A),
        ("TaskCreated", C),
        ("TaskStarted", C),
        ("ToolCalled", C),
        ("ToolSucceeded", C),
        ("FactAdded", C),
        ("TaskCompleted", C),
        ("TaskStarted", COMPARE),
        ("FactAdded", COMPARE),
        ("TaskCompleted", COMPARE),
    ]
    assert seq_of(events, EventType.TASK_COMPLETED, B) < seq_of(events, EventType.TASK_STARTED, COMPARE)

    failed = next(e for e in events if e.event_type is EventType.TASK_FAILED)
    assert (failed.payload.failure_type, failed.payload.error_type, failed.payload.tool_call_id) == (  # type: ignore[attr-defined]
        F.TOOL_FAILURE, "unavailable", f"{A}.t1",
    )
    [trigger] = [e for e in events if e.event_type is EventType.REPLAN_TRIGGERED]
    payload = trigger.payload
    assert isinstance(payload, ReplanTriggered)
    assert (payload.failed_task_id, payload.failure_type, payload.replan_number) == (A, F.TOOL_FAILURE, 1)
    assert payload.new_task_ids == [C] and payload.replacement_task_id == C
    assert payload.strategy_summary and "source C" in payload.strategy_summary
    assert (trigger.agent_id, trigger.task_id) == ("replanner", A)

    # Reconstruction from the log alone reproduces the whole recovery story.
    state = project(events)
    incremental = None
    for event in events:
        incremental = apply(incremental, event)
    assert state == incremental == await h.state(run_id)
    assert state.tasks[A].replaced_by == C and state.tasks[C].replaces == A
    assert state.tasks[A].failure is not None and state.tasks[A].failure.tool_call_id == f"{A}.t1"
    assert state.tool_calls[f"{A}.t1"].error_type == "unavailable"
    assert (state.recovery.replan_count, state.recovery.replan_attempts) == (1, 1)
    assert state.recovery.history[0].new_task_ids == (C,)

    # The replanner was told what failed, and the comparison ran on the replacement's result.
    [request] = h.replanner_calls()
    assert '"error_type": "unavailable"' in request.messages[0].content
    assert f'"failed_tool_arguments": "{{\\"query\\": \\"{QA}\\"' in request.messages[0].content
    analyst = next(r for r in h.llm.requests if r.purpose == "agent:analyst")
    assert "Product C is a candidate." in analyst.messages[0].content
    assert f'"replaces": "{A}"' in analyst.messages[0].content


# --- 15 / 23: agent timeout -> replan -> success ----------------------------------------------


async def test_agent_timeout_is_recovered_and_dependent_continues(database: Database) -> None:
    alt = replan(
        "The source A agent timed out; use source C instead.",
        replan_task(C, replaces=A, description="Research candidate products using source C instead."),
    )
    h = Harness(
        database, provider(in_order(alt), hang=[A]), search_backend(fail=[]), agent_timeout=0.5
    )
    run_id = await h.planned_run()

    report = await h.schedule(run_id)

    assert report.task_statuses == {A: S.FAILED, B: S.COMPLETED, C: S.COMPLETED, COMPARE: S.COMPLETED}
    state = await h.state(run_id)
    assert state.tasks[A].failure is not None
    assert (state.tasks[A].failure.failure_type, state.tasks[A].failure.error_type) == (F.TIMEOUT, "agent_timeout")
    assert "timed out" in (state.tasks[A].error or "")
    events = await h.events(run_id)
    assert seq_of(events, EventType.TASK_FAILED, A) < seq_of(events, EventType.REPLAN_TRIGGERED, A)
    assert seq_of(events, EventType.TASK_COMPLETED, C) < seq_of(events, EventType.TASK_STARTED, COMPARE)
    # Both attempts are in the history.
    assert [x for x in timeline(events) if x[0] in ("TaskStarted", "TaskFailed", "TaskCompleted") and x[1] in (A, C)] == [
        ("TaskStarted", A), ("TaskFailed", A), ("TaskStarted", C), ("TaskCompleted", C),
    ]


# --- 16 / 24: invalid replans never mutate the graph; run fails -------------------------------


INVALID = {
    "malformed": {"strategy_summary": "x", "tasks": "not a list"},
    "cyclic": replan("c", replan_task("x1", replaces=A, dependencies=["x2"]), replan_task("x2", dependencies=["x1"])),
    "unsupported_agent": replan("u", replan_task(C, replaces=A, agent_type="hacker")),
    "unsupported_task": replan("u", replan_task(C, replaces=A, task_type="exfiltrate")),
    "empty": replan("nothing"),
    "provider_error": LLMError("fake provider outage"),
}


@pytest.mark.parametrize("case", sorted(INVALID))
async def test_replan_failure_fails_the_run_without_mutating_the_graph(database: Database, case: str) -> None:
    h = Harness(database, provider(in_order(INVALID[case])), search_backend(fail=[QA]), max_replans=2)
    run_id = await h.planned_run()

    report = await h.schedule(run_id)

    assert report.task_statuses == {A: S.FAILED, B: S.COMPLETED, COMPARE: S.BLOCKED}  # no new tasks
    assert report.replanned == [] and report.run_status is RunStatus.FAILED
    events = await h.events(run_id)
    kinds = [e.event_type for e in events]
    assert kinds.count(EventType.TASK_CREATED) == 3
    assert kinds.count(EventType.REPLAN_REJECTED) == 2 and EventType.REPLAN_TRIGGERED not in kinds
    assert kinds[-1] is EventType.RUN_FAILED and kinds.count(EventType.TASK_FAILED) == 1  # no recursion
    state = project(events)
    assert state.status is RunStatus.FAILED and "replan budget exhausted" in (state.failure_reason or "")
    assert [r.outcome for r in state.recovery.history] == ["rejected", "rejected"]
    assert len(h.replanner_calls()) == 2


# --- 25 / 19: budget exhausted by repeated failures; repeated plans stop ----------------------


async def test_recovery_budget_exhausted_fails_the_run(database: Database) -> None:
    d = "research_source_d"
    replans = in_order(
        SOURCE_C_REPLAN,
        replan("Try source D.", replan_task(d, replaces=C, description="Research candidate products using source D.")),
        replan("Try source E.", replan_task("research_source_e", replaces=d, description="Use source E.")),
    )
    steps = {**STEPS, d: researcher_steps(d, "source D products", "Product D")}
    h = Harness(database, provider(replans, steps), search_backend(fail=[QA, QC, "source D products"]), max_replans=2)
    run_id = await h.planned_run()

    report = await h.schedule(run_id)

    assert report.task_statuses == {A: S.FAILED, B: S.COMPLETED, C: S.FAILED, d: S.FAILED, COMPARE: S.BLOCKED}
    assert report.replanned == [C, d] and report.run_status is RunStatus.FAILED
    assert len(h.replanner_calls()) == 2  # the third plan was never requested
    state = await h.state(run_id)
    assert (state.recovery.replan_count, state.recovery.replan_attempts) == (2, 2)
    assert state.failure_reason and "replan budget exhausted (2 of 2" in state.failure_reason
    assert f"{d!r} failed (TOOL_FAILURE: unavailable)" in state.failure_reason
    assert [e.event_type for e in await h.events(run_id)][-1] is EventType.RUN_FAILED


async def test_identical_replans_are_rejected_and_replanning_stops(database: Database) -> None:
    # The replanner keeps proposing the same thing; the replacement also fails.
    h = Harness(database, provider(in_order(SOURCE_C_REPLAN)), search_backend(fail=[QA, QC]), max_replans=3)
    run_id = await h.planned_run()

    report = await h.schedule(run_id)

    assert report.run_status is RunStatus.FAILED and report.replanned == [C]
    state = await h.state(run_id)
    assert [(r.outcome, r.summary.split(":")[0]) for r in state.recovery.history] == [
        ("accepted", "Source A is unavailable; research the same products using source C instead."),
        # The verbatim-repeated plan still targets A, not the task that failed now.
        ("rejected", "policy"),
        ("rejected", "policy"),
    ]
    assert len(h.replanner_calls()) == 3 and h.search.queries.count(QC) == 1


async def test_renamed_identical_replan_is_rejected_as_duplicate(database: Database) -> None:
    renamed = replan(
        "again",
        replan_task("research_source_c2", replaces=C, description=SOURCE_C_REPLAN["tasks"][0]["description"]),
    )
    renamed["tasks"][0]["title"] = SOURCE_C_REPLAN["tasks"][0]["title"]
    h = Harness(database, provider(in_order(SOURCE_C_REPLAN, renamed)), search_backend(fail=[QA, QC]), max_replans=2)
    run_id = await h.planned_run()

    await h.schedule(run_id)

    state = await h.state(run_id)
    [accepted, duplicate] = state.recovery.history
    assert duplicate.outcome == "rejected" and duplicate.summary.startswith("duplicate:")
    assert duplicate.plan_fingerprint == accepted.plan_fingerprint
    assert "research_source_c2" not in state.tasks


# --- ineligible failure: no replan, normal propagation ------------------------------------------


async def test_policy_failure_is_not_replanned(database: Database) -> None:
    steps = {**STEPS, A: [{"action": "call_tool", "tool_call": {"tool_name": "calculator", "arguments": {"expression": "1+1"}}, "report": None}]}
    h = Harness(database, provider(in_order(SOURCE_C_REPLAN), steps), search_backend(fail=[]))
    run_id = await h.planned_run()

    report = await h.schedule(run_id)

    assert report.task_statuses == {A: S.FAILED, B: S.COMPLETED, COMPARE: S.BLOCKED}
    assert report.run_status is RunStatus.CREATED  # propagation only, as in Phase 2
    assert h.replanner_calls() == []
    state = await h.state(run_id)
    assert state.tasks[A].failure is not None and state.tasks[A].failure.failure_type is F.POLICY_FAILURE
    assert state.recovery.replan_attempts == 0


async def test_without_recovery_manager_failures_only_propagate(database: Database) -> None:
    h = Harness(database, provider(in_order(SOURCE_C_REPLAN)), search_backend(fail=[QA]), recovery=False)
    run_id = await h.planned_run()

    report = await h.schedule(run_id)

    assert report.task_statuses == {A: S.FAILED, B: S.COMPLETED, COMPARE: S.BLOCKED}
    assert h.replanner_calls() == [] and report.run_status is RunStatus.CREATED


# --- 27 / 28: concurrency and event sequence ---------------------------------------------------


async def test_independent_replacement_tasks_run_concurrently(database: Database) -> None:
    c1, c2, merge = "search_source_c", "search_source_d", "merge_sources"
    qd = "source D products"
    structured = replan(
        "Source A is down: search sources C and D in parallel and merge them.",
        replan_task(c1, description="Search source C for candidate products."),
        replan_task(c2, description="Search source D for candidate products."),
        replan_task(merge, replaces=A, agent_type="analyst", task_type="analysis", dependencies=[c1, c2],
                    description="Merge the candidates found in sources C and D."),
    )
    steps = {
        **STEPS,
        c1: researcher_steps(c1, QC, "Product C"),
        c2: researcher_steps(c2, qd, "Product D"),
        merge: [{"action": "finish", "tool_call": None, "report": {"success": True, "summary": "C and D merged", "facts": [], "evidence": [], "artifacts": [], "error": None}}],
    }
    gates = {QC: asyncio.Event(), qd: asyncio.Event()}
    h = Harness(database, provider(in_order(structured), steps), search_backend(fail=[QA], gates=gates))
    run_id = await h.planned_run()

    scheduling = asyncio.create_task(h.scheduler.run(run_id))
    await asyncio.wait_for(h.search.wait_until_searched(QC), 10)
    await asyncio.wait_for(h.search.wait_until_searched(qd), 10)
    assert h.search.active == {QC, qd}  # both replacement searches in flight at once
    gates[QC].set()
    gates[qd].set()
    report = await asyncio.wait_for(scheduling, 10)

    assert report.task_statuses[merge] is S.COMPLETED and report.task_statuses[COMPARE] is S.COMPLETED
    assert report.replanned == [c1, c2, merge] and h.search.max_active == 2

    events = await h.events(run_id)
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    trigger = seq_of(events, EventType.REPLAN_TRIGGERED, A)
    assert seq_of(events, EventType.TASK_FAILED, A) < trigger
    for task_id in (c1, c2, merge):
        assert trigger < seq_of(events, EventType.TASK_CREATED, task_id) < seq_of(events, EventType.TASK_STARTED, task_id)
    assert seq_of(events, EventType.TASK_COMPLETED, merge) < seq_of(events, EventType.TASK_STARTED, COMPARE)
    assert project(events) == await h.state(run_id)


async def test_recovery_with_concurrent_task_completion_keeps_a_valid_sequence(database: Database) -> None:
    # B is still running (held by a gate) while A fails and is replanned.
    gate = asyncio.Event()
    h = Harness(database, provider(in_order(SOURCE_C_REPLAN)), search_backend(fail=[QA], gates={QB: gate}))
    run_id = await h.planned_run()

    scheduling = asyncio.create_task(h.scheduler.run(run_id))
    await asyncio.wait_for(h.search.wait_until_searched(QB), 10)
    for _ in range(200):
        if (await h.state(run_id)).recovery.replan_count:
            break
        await asyncio.sleep(0.01)
    state = await h.state(run_id)
    assert state.tasks[B].status is S.RUNNING and state.tasks[C].status in (S.READY, S.RUNNING, S.COMPLETED)
    gate.set()
    report = await asyncio.wait_for(scheduling, 10)

    assert set(report.task_statuses.values()) == {S.FAILED, S.COMPLETED}
    events = await h.events(run_id)
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert seq_of(events, EventType.REPLAN_TRIGGERED, A) < seq_of(events, EventType.TASK_COMPLETED, B)
    assert project(events) == await h.state(run_id)


# --- API ---------------------------------------------------------------------------------------


async def test_api_schedule_recovers_and_exposes_recovery_state(settings: Settings, database: Database) -> None:
    app = create_app(
        settings, database=database, llm_provider=provider(in_order(SOURCE_C_REPLAN)),
        tool_registry=fake_registry(search_backend(fail=[QA])),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        run_id = (await api.post("/api/v1/runs", json={"goal": GOAL})).json()["id"]
        base = f"/api/v1/runs/{run_id}"
        assert (await api.post(f"{base}/plan")).status_code == 201
        scheduled = await api.post(f"{base}/schedule")
        assert scheduled.status_code == 200, scheduled.text
        body = scheduled.json()
        assert body["replanned"] == [C] and body["task_statuses"][A] == "failed"
        assert body["task_statuses"][COMPARE] == "completed"

        state = (await api.get(f"{base}/state")).json()
        assert state["recovery"]["replan_count"] == 1
        assert state["tasks"][A]["failure"]["failure_type"] == "TOOL_FAILURE"
        assert state["tasks"][A]["replaced_by"] == C
        kinds = [e["event_type"] for e in (await api.get(f"{base}/events")).json()]
        assert "ReplanTriggered" in kinds and "RunFailed" not in kinds


async def test_api_schedule_with_recovery_disabled(settings: Settings, database: Database) -> None:
    app = create_app(
        settings, database=database, llm_provider=provider(None),
        tool_registry=fake_registry(search_backend(fail=[QA])),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        run_id = (await api.post("/api/v1/runs", json={"goal": GOAL})).json()["id"]
        base = f"/api/v1/runs/{run_id}"
        await api.post(f"{base}/plan")
        body = (await api.post(f"{base}/schedule", json={"recovery": False})).json()
        assert body["replanned"] == [] and body["task_statuses"][COMPARE] == "blocked"
        assert body["run_status"] == "created"


def test_plan_fixture_is_the_documented_scenario() -> None:
    assert [t["id"] for t in PLAN["tasks"]] == [A, B, COMPARE]


async def test_provider_rate_limit_fails_the_run_without_replanning(database: Database) -> None:
    """An LLM provider 429 is not a problem with the task: no replanner call, no budget spent,
    and the run fails with the provider reason instead of staying blocked."""
    agents = agent_steps(STEPS)

    def researcher(request: LLMRequest) -> FakeReply:
        if request.metadata["task_id"] == A:
            return FakeReply(error=LLMRateLimitError(
                "agent:researcher: the LLM provider is rate-limiting requests for model 'm' (HTTP 429, request r1); "
                "a provider availability problem, not a task problem"))
        return agents(request)

    llm = FakeLLMProvider({"planner": FakeReply(data=PLAN), "agent:researcher": researcher, "agent:analyst": agents,
                           "replanner": in_order(SOURCE_C_REPLAN)})
    h = Harness(database, llm, search_backend(fail=[]))
    run_id = await h.planned_run()

    report = await h.schedule(run_id)

    assert h.replanner_calls() == []
    assert report.run_status is RunStatus.FAILED and report.task_statuses[A] is S.FAILED
    state = await h.state(run_id)
    assert state.tasks[A].failure is not None
    assert (state.tasks[A].failure.failure_type, state.tasks[A].failure.error_type) == (F.PROVIDER_FAILURE, "llm_rate_limited")
    assert state.recovery.replan_attempts == 0
    [failed] = [e.payload for e in await h.events(run_id) if e.event_type is EventType.RUN_FAILED]
    assert "LLM provider unavailable" in failed.reason and "HTTP 429" in failed.reason  # type: ignore[attr-defined]
    assert "Not replanned" in failed.reason  # type: ignore[attr-defined]
