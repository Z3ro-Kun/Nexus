"""Phase 9 unit tests: planner action proposals (schema and gates), the derived run phase
and the final result. Pure: no database, no real LLM, no network.
"""

from typing import Any

import pytest

from app.agents.planner import PlannerAgent
from app.agents.registry import TASK_AGENT_SPECS
from app.core.exceptions import PlanRejectedError
from app.events.types import ActionCategory, FailureType, RunFailed, TaskFailed
from app.llm.fake import FakeLLMProvider, FakeReply
from app.llm.openai_provider import is_strict_compatible
from app.orchestration.orchestrator import CHECKPOINT_ID, objective_checkpoint
from app.orchestration.result import RunPhase, build_final_result, run_phase
from app.state.projector import project
from app.tools.fakes import FakeSideEffectTool
from tests.helpers import verified_completion
from tests.orchestration_fixtures import BUY, ORDER, plan
from tests.policy_fixtures import action
from tests.test_policy import requested
from tests.tool_fixtures import fake_registry
from tests.verification_fixtures import Log

ORDER_TOOL = FakeSideEffectTool(ORDER, ActionCategory.IRREVERSIBLE)


def planner(output: dict[str, Any], *, action_tools: bool = True) -> tuple[PlannerAgent, FakeLLMProvider]:
    llm = FakeLLMProvider({"planner": FakeReply(data=output)})
    tools = [*fake_registry().definitions(), ORDER_TOOL.definition] if action_tools else fake_registry().definitions()
    return PlannerAgent(llm, TASK_AGENT_SPECS, max_tokens=1000, action_tools=tools), llm


def action_plan(**changes: Any) -> dict[str, Any]:
    p = plan(with_action=True)
    p["actions"][0].update(changes)
    return p


# --- planner -> action tasks ---------------------------------------------------------------


async def test_planner_proposes_action_tasks() -> None:
    agent, llm = planner(action_plan())
    result = await agent.plan("goal", [], max_tasks=10)
    payloads = result.to_task_payloads()
    [buy] = [p for p in payloads if p.task_id == BUY]
    assert (buy.agent_type, buy.task_type, buy.dependencies) == ("action_executor", "action", ["compare"])
    assert buy.action is not None and (buy.action.tool_name, buy.action.arguments) == (ORDER, {"item": "Product X", "quantity": 1})
    [request] = llm.requests
    assert "Deterministic policy, not you, decides whether an action may run" in request.system
    assert "- place_order:" in request.system and "calculator" not in request.system.split("Actions:")[1]


def test_action_schema_is_closed_and_strict() -> None:
    schema = planner(plan(), action_tools=True)[0].output_schema()
    assert is_strict_compatible(schema) and schema["required"] == ["decision", "clarification", "tasks", "actions"]
    action_schema = schema["$defs"]["PlannedAction"]
    assert set(action_schema["properties"]) == {"id", "title", "description", "tool_name", "arguments", "dependencies"}
    assert action_schema["properties"]["tool_name"]["enum"] == [ORDER]  # read-only tools are for agents, not actions


def test_without_action_tools_the_schema_is_unchanged() -> None:
    schema = planner(plan(), action_tools=False)[0].output_schema()
    # Phase 11: the planner always decides explicitly (plan or needs_clarification).
    assert schema["required"] == ["decision", "clarification", "tasks"] and "actions" not in schema["properties"]
    assert set(schema["$defs"]) == {"PlannedTask", "ClarificationRequest"} and is_strict_compatible(schema)


@pytest.mark.parametrize(
    ("output", "match"),
    [
        (action_plan(category="read_only"), "schema"),
        (action_plan(approved=True), "schema"),
        (action_plan(policy="allow"), "schema"),
        (action_plan(arguments=[{"name": "item", "value": "X", "risk": "none"}]), "schema"),
        (action_plan(tool_name="calculator"), "not an action tool"),
        (action_plan(tool_name="wire_money"), "not an action tool"),
        (action_plan(arguments=[{"name": "item", "value": 5}]), "invalid arguments"),
        (action_plan(arguments=[{"name": "item", "value": "X"}, {"name": "approved", "value": True}]), "invalid arguments"),
        (action_plan(arguments=[{"name": "item", "value": "X"}, {"name": "item", "value": "Y"}]), "duplicate argument names"),
        (action_plan(dependencies=["nowhere"]), "missing_dependency"),
        (action_plan(id="compare"), "duplicate_task"),
    ],
    ids=["category", "approved", "policy", "argument-extra-field", "read-only-tool", "unknown-tool", "bad-argument-type",
         "smuggled-argument", "duplicate-argument", "unknown-dependency", "id-collision"],
)
async def test_untrusted_action_proposals_are_rejected(output: dict[str, Any], match: str) -> None:
    agent, _ = planner(output)
    with pytest.raises(PlanRejectedError, match=match):
        await agent.plan("goal", [], max_tasks=10)


async def test_actions_need_action_tools_and_count_towards_the_plan_size() -> None:
    with pytest.raises(PlanRejectedError, match="no action tools"):
        await planner(action_plan(), action_tools=False)[0].plan("goal", [], max_tasks=10)
    with pytest.raises(PlanRejectedError, match="the limit is 2"):
        await planner(action_plan())[0].plan("goal", [], max_tasks=3)


def test_checkpoint_requirements_come_from_the_run_not_the_model() -> None:
    state = Log(goal="Buy a laptop under budget.").state()
    spec = objective_checkpoint(state, semantic=True)
    assert (spec.objective, spec.semantic, spec.required_facts, spec.tool_evidence_tasks) == ("Buy a laptop under budget.", True, [], [])
    assert "." in CHECKPOINT_ID  # planner task ids cannot contain a dot, so they never collide


# --- run phase and final result --------------------------------------------------------------


def test_run_phases_are_derived_from_state() -> None:
    assert run_phase(Log().state()) is RunPhase.CREATED
    log = Log().create("ra")
    assert run_phase(log.state()) is RunPhase.EXECUTING
    log.checkpoint(deps=("ra",), spec_=objective_checkpoint(log.state(), semantic=False))
    log.start("ra").done("ra")
    assert run_phase(log.state()) is RunPhase.VERIFYING  # checkpoint ready
    claimed = Log(); claimed.events = list(log.events)
    claimed.start("verify")
    assert run_phase(claimed.state()) is RunPhase.VERIFYING  # claimed, before VerificationStarted
    log.begin()
    assert run_phase(log.state()) is RunPhase.VERIFYING  # VerificationStarted recorded

    waiting = Log().add(action("buy"))
    assert run_phase(requested(waiting).state()) is RunPhase.WAITING_FOR_APPROVAL

    blocked = Log().create("ra").start("ra")
    blocked.add(TaskFailed(task_id="ra", error="x", failure_type=FailureType.TOOL_FAILURE), task="ra")
    assert run_phase(blocked.state()) is RunPhase.BLOCKED
    assert run_phase(blocked.add(RunFailed(reason="x")).state()) is RunPhase.FAILED


def test_final_result_reports_deliverables_through_replacements() -> None:
    log = Log().create("ra").start("ra")
    log.add(TaskFailed(task_id="ra", error="x", failure_type=FailureType.TOOL_FAILURE, error_type="unavailable"), task="ra")
    log.create("rc", replaces="ra").start("rc").tool("rc").fact("rc.f1", "rc", 94999).done("rc", summary="found via C")
    log.create("compare", "ra", agent="analyst", task_type="analysis").start("compare").artifact("compare.a1", "compare").done("compare")
    events = log.events + verified_completion(log.events, summary="verified")
    result = build_final_result(project(events))

    assert result.phase is RunPhase.COMPLETED and result.verified and result.completion_summary == "verified"
    assert [d.task_id for d in result.deliverables] == ["compare"]  # rc feeds compare; ra is history
    assert [f.fact_id for f in result.supporting_facts] == ["rc.f1"] and result.supporting_facts[0].provenance_kind == "tool_output"
    assert {t.task_id: t.replaced_by for t in result.tasks}["ra"] == "rc"
    assert result.completion_blockers == [] and result.tool_calls["total"] == 1
