"""Phase 11: underspecified or conversational objectives stop at a clarification instead of
becoming an invented task graph.

The planner's *judgement* is a model's and is validated with a real LLM separately. These
tests script the planner's decision (FakeLLMProvider) and prove the contract around it:
the schema gate (a clarification never carries work), the prompt rules, and the
deterministic consequences enforced by NEXUS: no task, scheduler pass, agent, tool,
recovery, verification or artifact; a terminal needs_clarification run that is neither
completed nor failed; the goal unchanged; and normal planning (including parallel
decomposition) unchanged for actionable objectives.
"""

from typing import Any

import httpx
import pytest

from app.agents.planner import PlannerOutput
from app.agents.registry import AgentRegistry
from app.core.exceptions import InvalidEventError, PlanRejectedError
from app.core.config import Settings
from app.events.types import ClarificationRequested, RunCreated, TaskCreated
from app.llm.fake import FakeLLMProvider, FakeReply
from app.main import create_app
from app.models.run import RunStatus
from app.orchestration.result import RunPhase
from app.persistence.database import Database
from app.state.projector import project
from app.tools.artifact_write import ArtifactWriteTool
from app.artifacts.workspace import ArtifactWorkspace
from app.tools.executor import ToolExecutor
from app.tools.policy import ToolPolicy
from tests.agent_fixtures import planned
from tests.helpers import history
from tests.orchestration_fixtures import Pipeline, llm, plan as two_source_plan, steps
from tests.tool_fixtures import fake_registry


def clarify(question: str, *missing: str, reason: str = "underspecified") -> dict[str, Any]:
    return {"decision": "needs_clarification", "clarification": {"reason": reason, "question": question, "missing": list(missing)},
            "tasks": []}


UNDERSPECIFIED = {
    "I want 2 kids": clarify("What would you like NEXUS to do about having two children?", "what kind of help or result is wanted"),
    "I want a website": clarify("What should the website be for, and what do you want NEXUS to produce?", "purpose of the website", "the expected deliverable"),
    "I want to travel": clarify("What would you like NEXUS to help with for your travel?", "destination or kind of trip", "what result is wanted"),
    "Thanks": clarify("What would you like NEXUS to do?", "a request", reason="not_a_request"),
}


# --- the planner contract ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "match"),
    [
        ({**clarify("What do you want?", "intent"), "tasks": [planned("research_kids")]}, "must not include tasks"),
        ({"decision": "needs_clarification", "clarification": None, "tasks": []}, "requires a clarification"),
        ({"decision": "plan", "clarification": clarify("What exactly is wanted?", "x")["clarification"], "tasks": [planned("t1")]}, "must not include a clarification"),
        ({**clarify("What do you want?", "intent"), "actions": [{"id": "a", "title": "t", "description": "do the thing now", "tool_name": "x", "arguments": [], "dependencies": []}]}, "must not include tasks or actions"),
        (clarify("What do you want?"), "missing"),  # must say what is missing
    ],
    ids=["clarification-with-tasks", "clarification-without-question", "plan-with-clarification", "clarification-with-actions", "nothing-missing"],
)
async def test_a_clarification_can_never_carry_executable_work(output: dict[str, Any], match: str) -> None:
    planner = AgentRegistry(FakeLLMProvider({"planner": FakeReply(data=output)}), max_tokens=1000).planner()
    with pytest.raises(PlanRejectedError, match=match) as info:
        await planner.plan("I want 2 kids", [], max_tasks=10)
    assert info.value.stage == "schema"


def test_plan_only_outputs_remain_valid() -> None:
    assert PlannerOutput.model_validate({"tasks": [planned("t1")]}).decision == "plan"


async def test_planner_prompt_separates_intent_from_work_and_keeps_decomposition() -> None:
    provider = FakeLLMProvider({"planner": FakeReply(data=UNDERSPECIFIED["I want 2 kids"])})
    plan = await AgentRegistry(provider, max_tokens=1000).planner().plan("I want 2 kids", [], max_tasks=10)
    assert plan.needs_clarification and plan.tasks == () and plan.actions == ()
    request = provider.requests[0]
    system = request.system
    for rule in (
        "The objective is the user's intent, not automatically a task specification",
        "Do not turn a statement or preference into research, a plan or a deliverable the user did not request",
        "do not invent the missing constraints, outputs, decisions or actions",
        "When in doubt between an elaborate plan built on assumptions and a clarification",
        "Never plan tasks just to make a plan look thorough",
        "Reasonable defaults for minor details are fine",  # not over-conservative
        # Phase 10 decomposition rules are unchanged:
        "Identify the independent units of work in the goal",
        "Add a dependency only when a task actually requires another task's output",
    ):
        assert rule in system, rule
    assert request.output_schema["required"][:2] == ["decision", "clarification"]
    assert request.output_schema["properties"]["decision"]["enum"] == ["plan", "needs_clarification"]


# --- the deterministic boundary: nothing runs ------------------------------------------------------


@pytest.mark.parametrize("goal", list(UNDERSPECIFIED))
async def test_underspecified_or_conversational_objective_stops_before_any_work(database: Database, goal: str) -> None:
    p = Pipeline(database, llm(planner=UNDERSPECIFIED[goal]), semantic=True)
    run_id = await p.create(goal)
    result = await p.execute(run_id)
    state, events = await p.state(run_id), await p.events(run_id)

    expected = UNDERSPECIFIED[goal]["clarification"]
    assert result.phase is RunPhase.NEEDS_CLARIFICATION and (result.planned, result.passes) == (True, 0)
    assert state.status is RunStatus.NEEDS_CLARIFICATION  # not completed, not failed
    assert state.goal == goal and state.failure_reason is None and state.completion_summary is None
    assert [e.event_type.value for e in events] == ["RunCreated", "ClarificationRequested"]
    assert events[1].agent_id == "planner"
    assert (state.clarification.reason, state.clarification.question, list(state.clarification.missing)) == (  # type: ignore[union-attr]
        expected["reason"], expected["question"], expected["missing"])
    # No task, tool call, recovery, verification, conflict or artifact; only the planner was asked.
    assert state.tasks == {} and state.tool_calls == {} and state.verifications == {}
    assert state.workspace_artifacts == {} and state.artifacts == {} and state.conflicts == {}
    assert state.recovery.replan_attempts == 0
    assert [r.purpose for r in p.llm.requests] == ["planner"]
    assert (result.started, result.failed, result.replanned) == ([], [], [])
    final = result.result
    assert final.clarification is not None and final.clarification.question == expected["question"]
    assert final.completion_blockers == [] and not final.verified and final.deliverables == []
    assert project(events) == state

    # Executing again changes nothing and does not ask the planner again.
    again = await p.execute(run_id)
    assert again.phase is RunPhase.NEEDS_CLARIFICATION and not again.planned
    assert len(await p.events(run_id)) == 2 and len(p.llm.requests) == 1


def test_nothing_can_follow_a_clarification_and_it_cannot_follow_a_plan() -> None:
    run_id = __import__("uuid").uuid4()
    asked = ClarificationRequested(reason="underspecified", question="What would you like done?", missing=["intent"])
    with pytest.raises(InvalidEventError, match="needs_clarification; no further events allowed"):
        project(history(run_id, RunCreated(goal="I want 2 kids"), asked, TaskCreated(task_id="t1", title="Research")))
    with pytest.raises(InvalidEventError, match="cannot follow it"):
        project(history(run_id, RunCreated(goal="g"), TaskCreated(task_id="t1", title="Research"), asked))


async def test_api_exposes_the_clarification_and_refuses_execution_paths(database: Database, settings: Settings) -> None:
    provider = llm(planner=UNDERSPECIFIED["I want a website"])
    app = create_app(settings, database=database, llm_provider=provider, tool_registry=fake_registry())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        run_id = (await api.post("/api/v1/runs", json={"goal": "I want a website"})).json()["id"]
        planned_ = (await api.post(f"/api/v1/runs/{run_id}/plan")).json()
        assert planned_["tasks"] == [] and planned_["clarification"]["reason"] == "underspecified"

        run = (await api.get(f"/api/v1/runs/{run_id}")).json()
        assert run["status"] == "needs_clarification" and run["goal"] == "I want a website"
        result = (await api.get(f"/api/v1/runs/{run_id}/result")).json()
        assert result["phase"] == "needs_clarification"
        assert result["clarification"] == UNDERSPECIFIED["I want a website"]["clarification"] | {"reason": "underspecified"}
        assert (await api.post(f"/api/v1/runs/{run_id}/execute")).json()["phase"] == "needs_clarification"
        assert (await api.post(f"/api/v1/runs/{run_id}/schedule")).status_code == 409
        assert (await api.post(f"/api/v1/runs/{run_id}/plan")).status_code == 409
        forged = await api.post(f"/api/v1/runs/{run_id}/events", json={"events": [
            {"event_type": "TaskCreated", "payload": {"task_id": "t1", "title": "Research websites"}}]})
        assert forged.status_code == 422
        other = (await api.post("/api/v1/runs", json={"goal": "x"})).json()["id"]
        privileged = await api.post(f"/api/v1/runs/{other}/events", json={"events": [
            {"event_type": "ClarificationRequested", "payload": {"reason": "underspecified", "question": "Why?", "missing": ["x"]}}]})
        assert privileged.status_code == 403
    assert [r.purpose for r in provider.requests] == ["planner"]


# --- actionable objectives are planned exactly as before ------------------------------------------


async def test_actionable_objectives_still_get_normal_parallel_plans(database: Database) -> None:
    """The existing two-source scenario (research_a || research_b -> compare) runs unchanged
    when the planner decides "plan" explicitly, and still overlaps its independent tasks."""
    explicit = {"decision": "plan", "clarification": None, **two_source_plan()}
    p = Pipeline(database, llm(steps(), planner=explicit))
    run_id = await p.create("Research the best three laptops under $1000 and compare them.")
    result = await p.execute(run_id)
    state = await p.state(run_id)
    assert result.phase is RunPhase.COMPLETED and state.clarification is None
    assert [t for t in state.tasks if not state.tasks[t].dependencies] == ["research_a", "research_b"]
    assert state.tasks["compare"].dependencies == ("research_a", "research_b")


async def test_an_explicit_comparison_on_the_same_topic_is_planned() -> None:
    """The topic is not what decides: an explicit request about it is planned normally."""
    comparison = {"decision": "plan", "clarification": None, "tasks": [
        planned("adoption", description="Research what adopting two children involves."),
        planned("biological", description="Research what having two biological children involves."),
        planned("compare", agent_type="analyst", task_type="analysis", dependencies=["adoption", "biological"],
                description="Compare adoption and biological parenthood for having two children."),
    ]}
    planner = AgentRegistry(FakeLLMProvider({"planner": FakeReply(data=comparison)}), max_tokens=1000).planner()
    plan = await planner.plan("Compare adoption and biological parenthood for having two children.", [], max_tasks=10)
    assert not plan.needs_clarification and [t.id for t in plan.tasks if not t.dependencies] == ["adoption", "biological"]


async def test_generation_requests_keep_the_specialist_artifact_path(tmp_path: Any) -> None:
    tools = fake_registry()
    tools.register(ArtifactWriteTool(ArtifactWorkspace(tmp_path / "artifacts")))
    executor = ToolExecutor(tools, ToolPolicy.with_specialist_tools(["calculator", "artifact_write"]))
    build = {"decision": "plan", "clarification": None, "tasks": [planned(
        "write_bubble_sort", agent_type="specialist", task_type="domain_task",
        description="Write a Python bubble sort implementation and save it as bubble_sort.py.")]}
    provider = FakeLLMProvider({"planner": FakeReply(data=build)})
    planner = AgentRegistry(provider, max_tokens=1000, tool_executor=executor, max_tool_calls=3).planner()
    plan = await planner.plan("Create a Python bubble sort implementation and save it as bubble_sort.py.", [], max_tasks=10)
    assert [t.agent_type for t in plan.tasks] == ["specialist"]
    assert "specialist: applies domain expertise" in provider.requests[0].system
    assert "tools: artifact_write, calculator" in provider.requests[0].system
