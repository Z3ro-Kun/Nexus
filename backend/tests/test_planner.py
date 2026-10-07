"""Planner: LLM output is untrusted and must pass schema, policy and graph gates."""

import copy
from typing import Any

import pytest

from app.agents.planner import Plan
from app.agents.registry import AgentRegistry
from app.core.exceptions import LLMError, PlanRejectedError
from app.llm.fake import FakeLLMProvider, FakeReply
from tests.agent_fixtures import GOAL, RESEARCH_PLAN, planned
from tests.tool_fixtures import fake_executor

MAX_TASKS = 5


async def run_planner(data: Any = None, *, error: LLMError | None = None) -> tuple[Plan, FakeLLMProvider]:
    provider = FakeLLMProvider({"planner": FakeReply(data=data, error=error)})
    planner = AgentRegistry(provider, max_tokens=4000).planner()
    plan = await planner.plan(GOAL, ["Budget under $500"], max_tasks=MAX_TASKS)
    return plan, provider


async def rejected(data: Any) -> PlanRejectedError:
    with pytest.raises(PlanRejectedError) as info:
        await run_planner(data)
    return info.value


async def test_valid_output_produces_a_validated_graph() -> None:
    plan, provider = await run_planner(RESEARCH_PLAN)

    assert [t.id for t in plan.tasks] == ["research_candidates", "research_alternatives", "compare_results"]
    payloads = plan.to_task_payloads()
    assert payloads[2].dependencies == ["research_candidates", "research_alternatives"]
    assert payloads[2].agent_type == "analyst"

    [sent] = provider.requests
    assert sent.purpose == "planner"
    assert GOAL in sent.messages[0].content and "Budget under $500" in sent.messages[0].content
    task_schema = sent.output_schema["$defs"]["PlannedTask"]
    assert task_schema["properties"]["agent_type"]["enum"] == ["analyst", "researcher", "specialist"]
    assert task_schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "data",
    [
        {"tasks": [{"id": "t1"}]},  # missing fields
        {"plan": []},  # wrong top-level key
        {"tasks": "research things"},  # free text instead of tasks
        {"tasks": [planned("Bad ID!")]},  # invalid id format
        {"tasks": [planned("t1", description="short")]},  # description too short
        {"tasks": [planned("t1", command="rm -rf /")]},  # executable field smuggled in
        {"tasks": [planned("t1", url="http://example.com")]},
        {"tasks": [planned("t1")], "python": "import os"},
    ],
    ids=["missing-fields", "wrong-key", "free-text", "bad-id", "short-description", "command-field", "url-field", "extra-top-level"],
)
async def test_malformed_output_is_rejected(data: Any) -> None:
    error = await rejected(data)
    assert error.stage == "schema"


@pytest.mark.parametrize(
    ("task", "match"),
    [
        (planned("t1", agent_type="hacker"), "unsupported agent type 'hacker'"),
        (planned("t1", agent_type="planner"), "unsupported agent type 'planner'"),
        (planned("t1", task_type="shell_command"), "unsupported task type 'shell_command'"),
        (planned("t1", agent_type="researcher", task_type="analysis"), "does not accept task type"),
    ],
    ids=["unknown-agent", "planner-as-worker", "unknown-task-type", "agent-task-mismatch"],
)
async def test_policy_violations_are_rejected(task: dict[str, Any], match: str) -> None:
    error = await rejected({"tasks": [task]})
    assert error.stage == "policy"
    assert match in error.message


@pytest.mark.parametrize(
    ("tasks", "code"),
    [
        ([planned("t1"), planned("t1")], "duplicate_task"),
        ([planned("t1", dependencies=["t3"]), planned("t2", dependencies=["t1"]), planned("t3", dependencies=["t2"])], "dependency_cycle"),
        ([planned("t1", dependencies=["t1"])], "self_dependency"),
        ([planned("t1", dependencies=["ghost"])], "missing_dependency"),
    ],
    ids=["duplicate-ids", "cycle", "self-dependency", "missing-dependency"],
)
async def test_graph_violations_are_rejected(tasks: list[dict[str, Any]], code: str) -> None:
    error = await rejected({"tasks": tasks})
    assert error.stage == "graph"
    assert code in error.message


async def test_oversized_and_empty_plans_are_rejected() -> None:
    oversized = await rejected({"tasks": [planned(f"t{i}") for i in range(MAX_TASKS + 1)]})
    assert oversized.stage == "policy" and f"limit is {MAX_TASKS}" in oversized.message

    empty = await rejected({"tasks": []})
    assert empty.stage == "policy"


async def test_plan_at_the_limit_is_accepted() -> None:
    plan, _ = await run_planner({"tasks": [planned(f"t{i}") for i in range(MAX_TASKS)]})
    assert len(plan.tasks) == MAX_TASKS


async def test_provider_failure_propagates_as_llm_error() -> None:
    with pytest.raises(LLMError):
        await run_planner(error=LLMError("provider down"))


async def test_validation_does_not_modify_the_llm_output() -> None:
    data = copy.deepcopy(RESEARCH_PLAN)
    plan, _ = await run_planner(data)
    assert data == RESEARCH_PLAN
    assert [t.model_dump() for t in plan.tasks] == RESEARCH_PLAN["tasks"]


# --- Decomposition guidance --------------------------------------------------------------------


async def test_planner_is_asked_for_independent_tasks_not_the_fewest() -> None:
    _, provider = await run_planner(RESEARCH_PLAN)
    system = provider.requests[0].system

    # The rule: independent units -> separate tasks; a dependency only for a real input.
    assert "Identify the independent units of work" in system
    assert "Add a dependency only when a task actually requires another task's output" in system
    assert "depends on exactly the tasks whose results it combines" in system
    # Not a push towards more agents, nor towards merging independent work into one task.
    assert "More tasks are not better" in system and "never create two tasks that do the same work" in system
    assert "fewest" not in system and "small" not in system
    # Verification is NEXUS's own, never planned work.
    assert "NEXUS adds an independent verification" in system


async def test_planner_is_told_the_per_task_tool_budget_only_when_agents_have_tools() -> None:
    _, without_tools = await run_planner(RESEARCH_PLAN)
    assert "tool calls" not in without_tools.requests[0].system

    provider = FakeLLMProvider({"planner": FakeReply(data=RESEARCH_PLAN)})
    planner = AgentRegistry(provider, max_tokens=4000, tool_executor=fake_executor(), max_tool_calls=3).planner()
    await planner.plan(GOAL, [], max_tasks=MAX_TASKS)
    assert "Each task may make at most 3 tool calls" in provider.requests[0].system


async def test_a_plan_of_independent_units_converging_on_one_analyst_is_accepted() -> None:
    units = [f"price_{p}" for p in "wxyz"]
    plan, _ = await run_planner({"tasks": [
        *(planned(u) for u in units),
        planned("compare", agent_type="analyst", task_type="analysis", dependencies=units),
    ]})
    assert [t.id for t in plan.tasks if not t.dependencies] == units
    assert plan.to_task_payloads()[-1].dependencies == units
