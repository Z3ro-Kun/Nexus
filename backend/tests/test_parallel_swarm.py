"""Real parallel decomposition, end to end and deterministically: an objective with four
genuinely independent units of work runs as four concurrent research tasks that coordinate
only through shared state, converge on one analyst, and then on NEXUS's verification.

Everything goes through the real orchestrator, scheduler, agents, tool loop, conflict and
recovery managers and a real database. The planner, agents and replanner are scripted with
FakeLLMProvider; prices come from FakeSearchBackend. No real LLM, no network.

    research_w ─┐
    research_x ─┤
    research_y ─┼─> compare (analyst) ─> verify.objective (added by NEXUS)
    research_z ─┘
"""

import asyncio
from typing import Any

from app.events.base import Event
from app.events.types import ToolCalled
from app.orchestration.result import RunPhase, build_final_result
from app.persistence.database import Database
from app.state.models import TaskStatus
from app.state.projector import apply, project
from tests.agent_fixtures import planned
from tests.conflict_fixtures import QA, QB, researcher
from tests.orchestration_fixtures import VERIFY, Pipeline, compare_steps, in_order, llm

SWARM_GOAL = (
    "Find the current price of four laptops (Product W, Product X, Product Y and Product Z), "
    "compare them against a 100000 INR budget, and recommend one."
)
PRODUCTS = {"research_w": "Product W", "research_x": "Product X", "research_y": "Product Y", "research_z": "Product Z"}
PRICES = {"research_w": 104999, "research_x": 94999, "research_y": 89999, "research_z": 99999}
RESEARCH = list(PRODUCTS)
COMPARE = "compare"


def query(task_id: str) -> str:
    return f"{PRODUCTS[task_id].lower()} price"


def swarm_plan(research: list[str] = RESEARCH) -> dict[str, Any]:
    """What the planner should produce for SWARM_GOAL: one task per independent product,
    and one analyst that depends on exactly those tasks."""
    return {"tasks": [
        *(planned(t, title=f"Price of {PRODUCTS.get(t, t)}", description=f"Find the current price of {PRODUCTS.get(t, t)} (web search).") for t in research),
        planned(COMPARE, agent_type="analyst", task_type="analysis", dependencies=list(research),
                description="Compare the four prices against the 100000 INR budget and recommend one laptop."),
    ]}


def swarm_steps(**extra: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    research = {t: researcher(t, query(t), PRICES[t], subject=PRODUCTS[t]) for t in RESEARCH}
    return {**research, COMPARE: compare_steps("Product Y is the cheapest under budget."), **extra}


def kinds(events: list[Event], task: str) -> list[str]:
    return [e.event_type.value for e in events if e.task_id == task]


def first(events: list[Event], kind: str, task: str) -> int:
    return next(e.sequence for e in events if e.event_type.value == kind and e.task_id == task)


def started(events: list[Event]) -> list[str]:
    return [e.task_id for e in events if e.event_type.value == "TaskStarted"]  # type: ignore[misc]


async def test_independent_units_run_concurrently_and_converge_through_shared_state(database: Database) -> None:
    gates = {t: asyncio.Event() for t in RESEARCH}
    p = Pipeline(database, llm(swarm_steps(), planner=swarm_plan(), gates=gates))
    run_id = await p.create(SWARM_GOAL)
    execution = asyncio.create_task(p.execute(run_id))

    # All four research agents are working at the same moment, each held on its first turn.
    for t in RESEARCH:
        await p.llm.wait_until_called(t)
    assert p.llm.active == set(RESEARCH)
    mid = await p.state(run_id)
    assert {t: mid.tasks[t].status for t in RESEARCH} == dict.fromkeys(RESEARCH, TaskStatus.RUNNING)
    assert mid.tasks[COMPARE].status is TaskStatus.PENDING  # dependent work does not start early
    assert mid.tasks[VERIFY].status is TaskStatus.PENDING
    for gate in gates.values():
        gate.set()
    result = await execution

    state, events = await p.state(run_id), await p.events(run_id)
    assert result.phase is RunPhase.COMPLETED and result.result.verified
    assert p.llm.max_active == 4  # the four independent tasks, and nothing else, overlapped

    # Each task starts once and finishes once.
    for t in [*RESEARCH, COMPARE, VERIFY]:
        assert kinds(events, t).count("TaskStarted") == 1, t
        assert kinds(events, t).count("TaskCompleted") == 1, t
    # The analyst starts only after every input it combines; verification only after the analyst.
    assert all(first(events, "TaskStarted", COMPARE) > first(events, "TaskCompleted", t) for t in RESEARCH)
    assert first(events, "TaskStarted", VERIFY) > first(events, "TaskCompleted", COMPARE)
    assert state.tasks[VERIFY].dependencies == (*RESEARCH, COMPARE)

    # One tool call per research task, for its own product only: no duplicated or borrowed work.
    calls = [(e.task_id, e.payload.arguments["query"]) for e in events if isinstance(e.payload, ToolCalled)]
    assert sorted(calls) == sorted((t, query(t)) for t in RESEARCH)

    # Shared state holds each task's own evidence, with its provenance.
    for t in RESEARCH:
        fact = state.facts[f"{t}.f1"]
        assert (fact.task_id, fact.claim.subject, fact.claim.value) == (t, PRODUCTS[t], PRICES[t])  # type: ignore[union-attr]
        assert fact.provenance is not None and fact.provenance.kind == "tool_output" and fact.provenance.tool_name == "web_search"

    # Agents never message each other. A research agent sees only its own task; the analyst
    # receives the four results as projected state (dependency_results), not conversation.
    for t in RESEARCH:
        context = next(r for r in p.llm.requests if r.metadata.get("task_id") == t).messages[0].content
        assert '"dependency_results": []' in context
        # (The goal names all four products as background; no other task or its result is shown.)
        assert all(o not in context and f"price is {PRICES[o]}" not in context for o in RESEARCH if o != t)
    [analyst] = [r for r in p.llm.requests if r.metadata.get("task_id") == COMPARE]
    assert len(analyst.messages) == 1  # a single context message, no relayed dialogue
    for t in RESEARCH:
        assert f'"task_id": "{t}"' in analyst.messages[0].content
        assert f"{PRODUCTS[t]} price is {PRICES[t]}" in analyst.messages[0].content

    final = result.result
    assert [d.task_id for d in final.deliverables] == [COMPARE]
    assert {f.fact_id for f in final.supporting_facts} == {f"{t}.f1" for t in RESEARCH}

    # The event log alone reconstructs the state exactly, in one pass or incrementally.
    incremental = None
    for event in events:
        incremental = apply(incremental, event)
    assert project(events) == incremental == state
    assert build_final_result(state) == final
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))


async def test_a_dependency_chain_runs_one_task_at_a_time(database: Database) -> None:
    """The control case: the same machinery with A -> B -> C never overlaps."""
    chain = {"tasks": [
        planned("step_a", description="Find the current price of Product W (web search)."),
        planned("step_b", dependencies=["step_a"], description="Find the current price of Product X (web search)."),
        planned("step_c", dependencies=["step_b"], description="Find the current price of Product Y (web search)."),
    ]}
    steps = {s: researcher(s, f"{s} price", 1000 + i, subject=f"Item {s}") for i, s in enumerate(("step_a", "step_b", "step_c"))}
    p = Pipeline(database, llm(steps, planner=chain))
    run_id = await p.create(SWARM_GOAL)
    result = await p.execute(run_id)
    events = await p.events(run_id)

    assert result.phase is RunPhase.COMPLETED
    assert p.llm.max_active == 1
    assert started(events) == ["step_a", "step_b", "step_c", VERIFY]
    assert first(events, "TaskStarted", "step_b") > first(events, "TaskCompleted", "step_a")
    assert first(events, "TaskStarted", "step_c") > first(events, "TaskCompleted", "step_b")


async def test_one_failed_branch_is_replaced_without_rerunning_the_others(database: Database) -> None:
    replacement = {**planned("research_y_alt", description="Find the current price of Product Y from another source (web search)."),
                   "replaces": "research_y"}
    steps = swarm_steps(research_y_alt=researcher("research_y_alt", "product y price alternate", 89999, subject="Product Y"))
    replanner = in_order({"strategy_summary": "The search for Product Y failed; use another source.", "tasks": [replacement]})
    p = Pipeline(database, llm(steps, planner=swarm_plan(), replanner=replanner), search_failures=[query("research_y")])
    run_id = await p.create(SWARM_GOAL)
    result = await p.execute(run_id)
    state, events = await p.state(run_id), await p.events(run_id)

    assert result.phase is RunPhase.COMPLETED and result.replanned == ["research_y_alt"]
    assert state.tasks["research_y"].status is TaskStatus.FAILED and state.tasks["research_y"].replaced_by == "research_y_alt"
    # Successful independent work is kept: every task ran exactly once, none was restarted.
    assert sorted(started(events)) == sorted([*RESEARCH, "research_y_alt", COMPARE, VERIFY])
    for t in ("research_w", "research_x", "research_z"):
        assert kinds(events, t).count("TaskCompleted") == 1
    # The analyst waited for the replacement, and received its result in place of the failed task.
    assert first(events, "TaskStarted", COMPARE) > first(events, "TaskCompleted", "research_y_alt")
    [analyst] = [r for r in p.llm.requests if r.metadata.get("task_id") == COMPARE]
    assert '"task_id": "research_y_alt"' in analyst.messages[0].content and '"replaces": "research_y"' in analyst.messages[0].content
    assert project(events) == state


async def test_conflicting_independent_sources_are_resolved_before_verification(database: Database) -> None:
    """Two of the independent tasks are separate sources for the same product and disagree.
    The existing conflict system detects it from shared state and resolves it with new
    evidence before verification; the other branches are unaffected."""
    research = ["price_x_source_a", "price_x_source_b", "research_y", "research_z"]
    plan = swarm_plan(research)
    steps = {
        "price_x_source_a": researcher("price_x_source_a", QA, 94999),
        "price_x_source_b": researcher("price_x_source_b", QB, 99999),
        "research_y": researcher("research_y", query("research_y"), PRICES["research_y"], subject="Product Y"),
        "research_z": researcher("research_z", query("research_z"), PRICES["research_z"], subject="Product Z"),
        COMPARE: compare_steps(),
    }
    gates = {t: asyncio.Event() for t in research}
    p = Pipeline(database, llm(steps, planner=plan, gates=gates))
    run_id = await p.create(SWARM_GOAL)
    execution = asyncio.create_task(p.execute(run_id))
    for t in research:
        await p.llm.wait_until_called(t)
    assert p.llm.active == set(research)
    for gate in gates.values():
        gate.set()
    result = await execution
    state, events = await p.state(run_id), await p.events(run_id)

    [conflict] = result.result.conflicts
    assert result.phase is RunPhase.COMPLETED and conflict.status == "resolved"
    assert {state.facts[f].task_id for f in state.conflicts[conflict.conflict_id].fact_ids} == {"price_x_source_a", "price_x_source_b"}
    types = [e.event_type.value for e in events]
    assert types.index("ConflictResolved") < types.index("VerificationStarted")
    assert all(kinds(events, t).count("TaskStarted") == 1 for t in research)
    assert project(events) == state
