"""Phase 9 integration tests: one objective through the orchestrator, over a real database
(SQLite and, when configured, PostgreSQL 18). Planner, agents, replanner and semantic
verifier are FakeLLMProvider; tools are fakes. No real LLM, no network.
"""

import asyncio
from typing import Any

import httpx

from app.core.config import get_settings
from app.events.base import Event
from app.events.factory import parse_payload
from app.events.types import ActionCategory, ConflictResolved, ConflictUnresolved, FailureType, TaskFailed
from app.main import create_app
from app.orchestration.result import RunPhase, build_final_result
from app.persistence.database import Database
from app.state.projector import project
from app.tools.fakes import FakeSideEffectTool
from tests.agent_fixtures import planned
from tests.conflict_fixtures import COMPARE, QA, RA, RB, researcher, resolver_finds
from tests.orchestration_fixtures import (
    APPROVAL,
    BUY,
    GOAL,
    VERIFY,
    Pipeline,
    compare_steps,
    in_order,
    llm,
    plan,
    steps,
    verifier_says,
)
from tests.policy_fixtures import engine
from tests.recovery_fixtures import search_backend
from tests.tool_fixtures import fake_registry

CHECKPOINT2 = f"{VERIFY}_attempt2"


def kinds(events: list[Event], task: str | None = None) -> list[str]:
    return [e.event_type.value for e in events if task is None or e.task_id == task]


def count(events: list[Event], kind: str) -> int:
    return kinds(events).count(kind)


def assert_exactly_once(events: list[Event]) -> None:
    """Every task started at most once; one plan; contiguous sequences."""
    started = [e.payload.task_id for e in events if e.event_type.value == "TaskStarted"]  # type: ignore[attr-defined]
    assert len(started) == len(set(started))
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert count(events, "RunCompleted") <= 1


# --- A. happy path ---------------------------------------------------------------------------


async def test_objective_to_verified_completion(database: Database) -> None:
    gates = {RA: asyncio.Event(), RB: asyncio.Event()}
    p = Pipeline(database, llm(verifier=verifier_says(evidence=[f"{RA}.f1", "compare.a1"]), gates=gates), semantic=True)
    run_id = await p.create()
    execution = asyncio.create_task(p.execute(run_id))
    await p.llm.wait_until_called(RA)
    await p.llm.wait_until_called(RB)  # both research tasks are in flight at once
    mid = await p.state(run_id)
    assert mid.tasks[COMPARE].status.value == "pending"  # the analyst waits for both
    for gate in gates.values():
        gate.set()
    result = await execution

    state, events = await p.state(run_id), await p.events(run_id)
    assert (result.phase, result.planned, state.status.value) == (RunPhase.COMPLETED, True, "completed")
    assert p.llm.max_active >= 2
    # The plan and the checkpoint arrive in one append; the checkpoint is NEXUS's, over all planned work.
    created = [e for e in events if e.event_type.value == "TaskCreated"]
    assert [e.payload.task_id for e in created] == [RA, RB, COMPARE, VERIFY]  # type: ignore[attr-defined]
    assert state.tasks[VERIFY].dependencies == (RA, RB, COMPARE) and state.tasks[VERIFY].verification.semantic  # type: ignore[union-attr]
    assert kinds(events).index("TaskCompleted") < kinds(events).index("VerificationPassed") < kinds(events).index("RunCompleted")
    final = result.result
    assert final.verified and [d.task_id for d in final.deliverables] == [COMPARE]
    assert final.deliverables[0].artifacts[0].name == "recommendation"
    assert {f.fact_id for f in final.supporting_facts} == {f"{RA}.f1", f"{RB}.f1"}
    assert all(f.provenance_kind == "tool_output" and f.tool_name == "web_search" for f in final.supporting_facts)
    assert final.verifications[0].semantic == "passed" and final.tool_calls["by_tool"] == {"web_search": 2}
    assert_exactly_once(events)
    assert project(events) == state and build_final_result(state) == final


# --- B. failure -> recovery -----------------------------------------------------------------


async def test_failed_task_is_replaced_and_the_run_still_completes(database: Database) -> None:
    replacement = {**planned("research_c", description="Find Product X's price using source C instead of source A."), "replaces": RA}
    agent_steps = steps(research_c=researcher("research_c", "product x price source c", 94999))
    p = Pipeline(database, llm(agent_steps, replanner=in_order({"strategy_summary": "Source A is down; use source C.", "tasks": [replacement]})),
                 search_failures=[QA])
    run_id = await p.create()
    result = await p.execute(run_id)
    state, events = await p.state(run_id), await p.events(run_id)

    assert result.phase is RunPhase.COMPLETED and result.replanned == ["research_c"]
    assert state.tasks[RA].status.value == "failed" and state.tasks[RA].replaced_by == "research_c"  # history kept
    assert state.verifications[VERIFY].covered_task_ids == (COMPARE, RB, "research_c")
    assert [r.failed_task_id for r in result.result.recovery] == [RA]
    assert_exactly_once(events)


# --- C/D. conflicts -------------------------------------------------------------------------


async def test_conflict_is_resolved_before_verification(database: Database) -> None:
    p = Pipeline(database, llm(steps(94999, 99999)))
    run_id = await p.create()
    result = await p.execute(run_id)
    state, events = await p.state(run_id), await p.events(run_id)

    [conflict] = result.result.conflicts
    assert result.phase is RunPhase.COMPLETED and conflict.status == "resolved"
    assert any(isinstance(e.payload, ConflictResolved) for e in events)
    assert kinds(events).index("ConflictResolved") < kinds(events).index("VerificationStarted")
    assert state.facts[f"{RA}.f1"].claim.value == 94999 and state.facts[f"{RB}.f1"].claim.value == 99999  # type: ignore[union-attr]
    assert conflict.accepted_fact_id in {f.fact_id for f in result.result.supporting_facts}


async def test_unresolved_conflict_never_completes(database: Database) -> None:
    remediation = {"strategy_summary": "Re-check the comparison.", "tasks": [
        {**planned("recheck", agent_type="analyst", task_type="analysis", dependencies=[COMPARE],
                   description="Re-check the comparison of the disputed prices."), "replaces": None}]}
    p = Pipeline(database, llm(steps(94999, 99999, recheck=compare_steps()), resolvers=resolver_finds(QA, 94999),
                               replanner=in_order(remediation)), max_replans=1)
    run_id = await p.create()
    result = await p.execute(run_id)
    state, events = await p.state(run_id), await p.events(run_id)

    assert any(isinstance(e.payload, ConflictUnresolved) for e in events)
    assert not any(isinstance(e.payload, ConflictResolved) for e in events)  # no silent winner
    assert result.phase is RunPhase.FAILED and count(events, "RunCompleted") == 0 and not result.result.verified
    assert [v.status.value for v in result.result.verifications] == ["failed", "failed"]
    assert "replan budget exhausted" in (state.failure_reason or "")


# --- E/K. approval pause / resume -----------------------------------------------------------


async def test_action_waits_for_approval_then_executes_once_and_completes(database: Database) -> None:
    p = Pipeline(database, llm(planner=plan(with_action=True)))
    run_id = await p.create()

    first = await p.execute(run_id)
    assert (first.phase, first.approvals_pending, p.order.count) == (RunPhase.WAITING_FOR_APPROVAL, [APPROVAL], 0)
    waiting = await p.state(run_id)
    assert waiting.tasks[BUY].action is not None and waiting.tasks[BUY].action.arguments == {"item": "Product X", "quantity": 1}  # type: ignore[union-attr]
    assert waiting.policy[BUY].decision.category.value == "irreversible"  # type: ignore[union-attr]

    for _ in range(2):  # repeated calls while waiting change nothing
        again = await p.execute(run_id)
        assert (again.phase, again.started, p.order.count) == (RunPhase.WAITING_FOR_APPROVAL, [], 0)
    events = await p.events(run_id)
    assert (count(events, "PolicyEvaluated"), count(events, "ApprovalRequested"), count(events, "TaskCreated")) == (1, 1, 5)

    await p.approvals.decide(run_id, APPROVAL, granted=True, actor="alice")
    assert p.order.count == 0  # approving executes nothing
    done = await p.execute(run_id)
    events = await p.events(run_id)
    assert (done.phase, p.order.count, done.planned) == (RunPhase.COMPLETED, 1, False)
    order = kinds(events)
    assert order.index("ApprovalGranted") < order.index("ToolSucceeded", order.index("ApprovalGranted")) < order.index("VerificationPassed") < order.index("RunCompleted")
    assert [d.task_id for d in done.result.deliverables] == [COMPARE]  # the order is an effect, not a deliverable
    [approval] = done.result.approvals
    assert (approval.status.value, approval.executed, approval.actor) == ("granted", True, "alice")
    actions_check = next(c for c in (await p.state(run_id)).verifications[VERIFY].checks if c.check_id == "actions")
    assert actions_check.passed
    await p.execute(run_id)
    assert p.order.count == 1 and count(await p.events(run_id), "RunCompleted") == 1


async def test_rejected_action_is_recovered_without_executing(database: Database) -> None:
    alternative = {**planned("document_choice", agent_type="analyst", task_type="analysis", dependencies=[COMPARE],
                             description="Document the recommendation for a manual purchase instead."), "replaces": BUY}
    p = Pipeline(database, llm(steps(document_choice=compare_steps("Recorded for manual purchase.")), planner=plan(with_action=True),
                               replanner=in_order({"strategy_summary": "The order was rejected; document instead.", "tasks": [alternative]})))
    run_id = await p.create()
    await p.execute(run_id)
    await p.approvals.decide(run_id, APPROVAL, granted=False, actor="alice", reason="buy it manually")
    result = await p.execute(run_id)
    state, events = await p.state(run_id), await p.events(run_id)

    assert p.order.count == 0 and result.phase is RunPhase.COMPLETED
    failed = next(e.payload for e in events if isinstance(e.payload, TaskFailed) and e.payload.task_id == BUY)
    assert (failed.failure_type, failed.error_type) == (FailureType.POLICY_FAILURE, "approval_rejected")
    assert state.tasks[BUY].replaced_by == "document_choice" and len(p.calls("replanner")) == 1
    assert result.result.approvals[0].executed is False


async def test_denied_action_is_never_executed_or_routed_around(database: Database) -> None:
    p = Pipeline(database, llm(planner=plan(with_action=True), replanner=in_order({"strategy_summary": "x", "tasks": []})),
                 policy=engine(irreversible="deny"))
    run_id = await p.create()
    result = await p.execute(run_id)
    state = await p.state(run_id)

    assert p.order.count == 0 and state.policy[BUY].decision.outcome.value == "deny"
    assert p.calls("replanner") == [] and result.phase is RunPhase.BLOCKED
    assert state.tasks[VERIFY].status.value == "blocked" and state.status.value == "created"
    assert "action buy_laptop was denied and not replaced" in result.result.completion_blockers


# --- H. verification failure -> remediation -------------------------------------------------


async def test_failed_verification_is_remediated_through_recovery(database: Database) -> None:
    remediation = {"strategy_summary": "Add the missing comparison of warranties.", "tasks": [
        {**planned("compare_warranty", agent_type="analyst", task_type="analysis", dependencies=[COMPARE],
                   description="Compare the warranties of the candidate laptops."), "replaces": None}]}
    verifier = in_order(verifier_says("fail", [], "The recommendation ignores warranty."),
                        verifier_says(evidence=["compare_warranty"]))
    p = Pipeline(database, llm(steps(compare_warranty=compare_steps("Warranties compared.")), verifier=verifier,
                               replanner=in_order(remediation)), semantic=True)
    run_id = await p.create()
    result = await p.execute(run_id)
    state = await p.state(run_id)

    assert result.phase is RunPhase.COMPLETED
    assert [(v.verification_id, v.status.value) for v in result.result.verifications] == [(VERIFY, "failed"), (CHECKPOINT2, "passed")]
    assert state.tasks[CHECKPOINT2].verification == state.tasks[VERIFY].verification  # requirements not weakened
    assert "compare_warranty" in state.tasks[CHECKPOINT2].dependencies


# --- I/J. idempotency and concurrency --------------------------------------------------------


async def test_repeated_execution_is_idempotent(database: Database) -> None:
    p = Pipeline(database, llm())
    run_id = await p.create()
    first = await p.execute(run_id)
    events = await p.events(run_id)
    for _ in range(3):
        again = await p.execute(run_id)
        assert (again.phase, again.planned, again.passes, again.started) == (RunPhase.COMPLETED, False, 0, [])
    assert first.phase is RunPhase.COMPLETED and await p.events(run_id) == events
    assert len(p.calls("planner")) == 1


async def test_competing_orchestrators_execute_everything_once(database: Database) -> None:
    p = Pipeline(database, llm(planner=plan(with_action=True)))
    run_id = await p.create()
    await asyncio.gather(*(p.execute(run_id) for _ in range(3)))
    events = await p.events(run_id)
    assert count(events, "TaskCreated") == 5  # one plan (+ checkpoint), however many planners raced
    assert (count(events, "PolicyEvaluated"), count(events, "ApprovalRequested"), p.order.count) == (1, 1, 0)

    await p.approvals.decide(run_id, APPROVAL, granted=True, actor="alice")
    results = await asyncio.gather(*(p.execute(run_id) for _ in range(3)))
    events = await p.events(run_id)
    # A concurrent call may return while another still holds the work; the run itself must complete.
    assert {r.phase for r in results} <= {RunPhase.EXECUTING, RunPhase.VERIFYING, RunPhase.COMPLETED}
    assert (await p.state(run_id)).status.value == "completed" and p.order.count == 1
    assert (count(events, "VerificationPassed"), count(events, "RunCompleted")) == (1, 1)
    assert_exactly_once(events)
    assert project(events) == await p.state(run_id)


# --- API ---------------------------------------------------------------------------------------


async def test_api_execute_pause_approve_resume(database: Database, settings: Any) -> None:
    provider = llm(planner=plan(with_action=True))
    order = FakeSideEffectTool("place_order", ActionCategory.IRREVERSIBLE)
    registry = fake_registry(search_backend(fail=[]))
    registry.register(order)
    settings = settings.model_copy(update={"verification_semantic": False, "max_tool_calls_per_task": 3})
    app = create_app(settings, database=database, llm_provider=provider, tool_registry=registry)
    app.dependency_overrides[get_settings] = lambda: settings
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        run_id = (await api.post("/api/v1/runs", json={"goal": GOAL})).json()["id"]
        base = f"/api/v1/runs/{run_id}"
        first = (await api.post(f"{base}/execute")).json()
        assert (first["phase"], first["approvals_pending"], order.count) == ("waiting_for_approval", [APPROVAL], 0)
        [view] = (await api.get(f"{base}/approvals")).json()
        assert view["decision"]["category"] == "irreversible" and view["context"][0]["task_id"] == COMPARE
        assert (await api.post(f"{base}/approvals/{APPROVAL}/approve", json={"actor": "alice"})).status_code == 200
        done = (await api.post(f"{base}/execute")).json()
        result = (await api.get(f"{base}/result")).json()
        api_state = (await api.get(f"{base}/state")).json()
        raw = (await api.get(f"{base}/events")).json()

    assert (done["phase"], order.count, result["verified"]) == ("completed", 1, True)
    assert result == done["result"] and result["approvals"][0]["executed"] is True
    events = [Event.model_validate({**e, "payload": parse_payload(e["event_type"], e["payload"])}) for e in raw]
    assert project(events).model_dump(mode="json") == api_state


async def test_api_execute_continues_a_run_created_with_tasks(api: httpx.AsyncClient) -> None:
    run_id = (await api.post("/api/v1/runs", json={"goal": "g"})).json()["id"]
    base = f"/api/v1/runs/{run_id}"
    await api.post(f"{base}/tasks", json={"tasks": [{"task_id": "a", "title": "a"}, {"task_id": "b", "title": "b", "dependencies": ["a"]}]})
    done = (await api.post(f"{base}/execute", json={"executor": "scripted"})).json()
    assert done["phase"] == "completed" and [v["verification_id"] for v in done["result"]["verifications"]] == [VERIFY]
    again = (await api.post(f"{base}/execute", json={"executor": "scripted"})).json()
    assert again["passes"] == 0 and again["result"]["last_sequence"] == done["result"]["last_sequence"]


async def test_api_execute_without_a_planner_for_an_empty_run(api: httpx.AsyncClient) -> None:
    run_id = (await api.post("/api/v1/runs", json={"goal": "g"})).json()["id"]
    response = await api.post(f"/api/v1/runs/{run_id}/execute", json={"executor": "scripted"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "llm_not_configured"

