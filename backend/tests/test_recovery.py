"""Phase 5 unit tests: failure classification, recovery policy, replanner validation,
graph evolution (replacement), projection of recovery events, and safety limits.

No database, no network, no real LLM: FakeLLMProvider scripts the replanner.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from app.agents.registry import TASK_AGENT_SPECS, AgentRegistry
from app.agents.replanner import ReplanTask, ReplannerAgent, ReplanRejectedError, plan_fingerprint, ReplannerOutput
from app.core.exceptions import (
    DependencyCycleError,
    InvalidEventError,
    InvalidReplacementError,
    UnknownAgentTypeError,
)
from app.core.redaction import safe_message
from app.events.types import (
    FailureType,
    ReplanRejected,
    ReplanTriggered,
    RunCreated,
    TaskCompleted,
    TaskCreated,
    TaskFailed,
    TaskStarted,
    ToolCalled,
    ToolFailed,
)
from app.llm.fake import FakeLLMProvider, FakeReply
from app.orchestration.task_executor import TaskExecutionResult
from app.orchestration.task_graph import TaskGraph
from app.recovery.classifier import blocked_tasks, classify_result, failure_record
from app.recovery.context import build_recovery_context
from app.recovery.policy import RecoveryPolicy
from app.recovery.schemas import FailureRecord, RecoveryAction
from app.state.context_builder import build_task_context
from app.state.models import RunState, TaskStatus
from app.state.projector import project
from tests.helpers import history
from tests.recovery_fixtures import A, B, C, COMPARE, SOURCE_C_REPLAN, replan, replan_task

S = TaskStatus
F = FailureType
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def tool_failure(tool_error_type: str) -> TaskExecutionResult:
    return TaskExecutionResult(
        succeeded=False,
        error=f"tool call t.t1 (web_search) failed [{tool_error_type}]: nope",
        error_type="tool_failed",
        tool_call_id="t.t1",
        events=[
            ToolCalled(tool_call_id="t.t1", tool_name="web_search", arguments={"query": "q"}),
            ToolFailed(tool_call_id="t.t1", error="nope", error_type=tool_error_type),
        ],
    )


# --- 1-4: failure classification ----------------------------------------------------------


def test_tool_failure_is_classified_as_tool_failure() -> None:
    c = classify_result(tool_failure("unavailable"))
    assert (c.failure_type, c.error_type, c.tool_call_id) == (F.TOOL_FAILURE, "unavailable", "t.t1")


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        ("agent_reported_failure", F.AGENT_FAILURE),
        ("llm_error", F.AGENT_FAILURE),
        ("tool_limit", F.AGENT_FAILURE),
        ("malformed_result", F.VALIDATION_FAILURE),
        ("invalid_provenance", F.VALIDATION_FAILURE),
        ("llm_invalid_response", F.VALIDATION_FAILURE),
        ("unknown_agent", F.PLANNING_FAILURE),
        ("unsupported_task_type", F.PLANNING_FAILURE),
        (None, F.AGENT_FAILURE),  # e.g. the scripted executor: unclassified
    ],
)
def test_agent_failures_are_classified(error_type: str | None, expected: FailureType) -> None:
    c = classify_result(TaskExecutionResult.failure("boom", error_type=error_type))
    assert c.failure_type is expected
    assert c.error_type == (error_type or "unclassified") and c.tool_call_id is None


@pytest.mark.parametrize(
    "result",
    [
        TaskExecutionResult.failure("agent timed out after 1s", error_type="agent_timeout"),
        TaskExecutionResult.failure("llm_timeout: slow", error_type="llm_timeout"),
        tool_failure("timeout"),
    ],
)
def test_timeouts_are_classified_as_timeout(result: TaskExecutionResult) -> None:
    assert classify_result(result).failure_type is F.TIMEOUT


@pytest.mark.parametrize(
    "result",
    [
        tool_failure("unauthorized"),
        tool_failure("ssrf_blocked"),
        TaskExecutionResult.failure("x", error_type="disallowed_events"),
    ],
)
def test_policy_failures_are_classified_as_policy_failure(result: TaskExecutionResult) -> None:
    assert classify_result(result).failure_type is F.POLICY_FAILURE


def test_failure_messages_are_redacted_and_truncated() -> None:
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
    message = (
        f"fetch https://user:hunter2@api.example.com/?api_key=XYZ123 failed; "
        f"Authorization: Bearer abcdefghijklmnop; key {secret}; password=pa55word " + "x" * 5000
    )
    c = classify_result(TaskExecutionResult.failure(message, error_type="llm_error"))
    for leaked in ("hunter2", "XYZ123", "abcdefghijklmnop", secret, "pa55word"):
        assert leaked not in c.message
    assert len(c.message) <= 2000 and c.message.endswith("[truncated]")
    assert safe_message("tool-call limit of 3 per task reached") == "tool-call limit of 3 per task reached"


# --- state helpers ----------------------------------------------------------------------------


def run_state(*payloads: Any) -> RunState:
    return project(history(uuid4(), RunCreated(goal="g"), *payloads))


def created(task_id: str, *deps: str, replaces: str | None = None, **kw: Any) -> TaskCreated:
    return TaskCreated(
        task_id=task_id, title=task_id, agent_type=kw.get("agent_type", "researcher"),
        task_type=kw.get("task_type", "research"), description=kw.get("description", f"do {task_id}"),
        dependencies=list(deps), replaces=replaces,
    )


def failed(task_id: str, failure_type: FailureType = F.TOOL_FAILURE, error_type: str = "unavailable") -> list[Any]:
    return [
        TaskStarted(task_id=task_id),
        TaskFailed(task_id=task_id, error="failed", failure_type=failure_type, error_type=error_type),
    ]


def completed(task_id: str) -> list[Any]:
    return [TaskStarted(task_id=task_id), TaskCompleted(task_id=task_id, summary=f"{task_id} ok")]


def base_state(failure_type: FailureType = F.TOOL_FAILURE, error_type: str = "unavailable") -> RunState:
    return run_state(
        created(A), created(B), created(COMPARE, A, B, agent_type="analyst", task_type="analysis"),
        *failed(A, failure_type, error_type), *completed(B),
    )


def triggered(n: int, failed_id: str = A, new: tuple[str, ...] = (C,)) -> ReplanTriggered:
    return ReplanTriggered(
        reason="r", failed_task_id=failed_id, failure_type=F.TOOL_FAILURE, replan_number=n,
        strategy_summary="s", new_task_ids=list(new), replacement_task_id=new[-1], plan_fingerprint=f"fp{n}",
    )


def rejected(n: int, failed_id: str = A) -> ReplanRejected:
    return ReplanRejected(
        failed_task_id=failed_id, failure_type=F.TOOL_FAILURE, replan_number=n, stage="schema", reason="bad"
    )


# --- 5-7: recovery policy -------------------------------------------------------------------


def test_eligible_failure_allows_replan() -> None:
    state = base_state()
    decision = RecoveryPolicy(2).decide(failure_record(state, A), state)
    assert decision.action is RecoveryAction.REPLAN


@pytest.mark.parametrize(
    ("failure_type", "error_type"),
    [(F.POLICY_FAILURE, "unauthorized"), (F.DEPENDENCY_FAILURE, "blocked"), (F.AGENT_FAILURE, "executor_error")],
)
def test_ineligible_failure_does_not_replan(failure_type: FailureType, error_type: str) -> None:
    state = base_state(failure_type, error_type)
    decision = RecoveryPolicy(2).decide(failure_record(state, A), state)
    assert decision.action is RecoveryAction.PROPAGATE


def test_replan_budget_is_enforced_on_every_replanner_invocation() -> None:
    policy = RecoveryPolicy(2)
    one_rejected = run_state(*base_state_events(), rejected(1))
    assert policy.remaining(one_rejected) == 1
    assert policy.decide(failure_record(one_rejected, A), one_rejected).action is RecoveryAction.REPLAN

    spent = run_state(*base_state_events(), rejected(1), rejected(2))
    decision = policy.decide(failure_record(spent, A), spent)
    assert decision.action is RecoveryAction.FAIL_RUN and "budget exhausted (2 of 2" in decision.reason
    zero = base_state()
    assert RecoveryPolicy(0).decide(failure_record(zero, A), zero).action is RecoveryAction.FAIL_RUN


def base_state_events() -> list[Any]:
    return [
        created(A), created(B), created(COMPARE, A, B, agent_type="analyst", task_type="analysis"),
        *failed(A), *completed(B),
    ]


def test_already_replaced_task_is_not_replanned_again() -> None:
    state = run_state(*base_state_events(), triggered(1), created(C, replaces=A))
    assert RecoveryPolicy(3).decide(failure_record(state, A), state).action is RecoveryAction.PROPAGATE


# --- 8-13, 20, 21: replanner validation -------------------------------------------------------


def replanner(reply: dict[str, Any] | None = None, error: Exception | None = None) -> tuple[ReplannerAgent, FakeLLMProvider]:
    llm = FakeLLMProvider({"replanner": FakeReply(data=reply, error=error)})  # type: ignore[arg-type]
    return ReplannerAgent(llm, TASK_AGENT_SPECS, max_tokens=1000, tools_by_agent={"researcher": ["web_search"]}), llm


async def propose(reply: dict[str, Any], state: RunState | None = None, previous: set[str] | None = None, max_new: int = 3) -> Any:
    state = state or base_state()
    agent, llm = replanner(reply)
    context = build_recovery_context(state, failure_record(state, A), agent.capabilities(), max_replans=2, max_new_tasks=max_new)
    proposal = await agent.replan(context, graph=TaskGraph.from_tasks(state.tasks.values()), previous_fingerprints=previous or set())
    return proposal, llm


async def test_valid_replan_is_accepted() -> None:
    proposal, llm = await propose(SOURCE_C_REPLAN)

    assert proposal.replacement_task_id == C and proposal.failed_task_id == A
    assert [p.replaces for p in proposal.to_task_payloads()] == [A]
    # The replanner saw the failure, the plan, the completed results and the budget.
    [request] = llm.requests
    content = request.messages[0].content
    for expected in ('"failure_type": "TOOL_FAILURE"', f'"task_id": "{B}"', "b ok", '"replans_remaining_after_this": 1', '"blocked_by"'):
        assert expected in content
    assert request.output_schema["$defs"]["ReplanTask"]["properties"]["agent_type"]["enum"] == ["analyst", "researcher", "specialist"]


INVALID_REPLANS = {
    "malformed": ({"strategy_summary": "x", "tasks": [{"id": "Bad Id!"}]}, "schema"),
    "missing_summary": ({"tasks": [replan_task(C, replaces=A)]}, "schema"),
    "cyclic": (
        replan("cycle", replan_task("x1", replaces=A, dependencies=["x2"]), replan_task("x2", dependencies=["x1"])),
        "graph",
    ),
    "unsupported_agent": (replan("s", replan_task(C, replaces=A, agent_type="hacker")), "policy"),
    "unsupported_task": (replan("s", replan_task(C, replaces=A, task_type="exfiltrate")), "policy"),
    "agent_task_mismatch": (replan("s", replan_task(C, replaces=A, task_type="analysis")), "policy"),
    "empty": (replan("nothing to do"), "policy"),
    "no_replacement": (replan("s", replan_task(C)), "policy"),
    "replaces_wrong_task": (replan("s", replan_task(C, replaces=B)), "policy"),
    "two_replacements": (replan("s", replan_task(C, replaces=A), replan_task("d", replaces=A)), "policy"),
    "id_collision": (replan("s", replan_task(B, replaces=A)), "graph"),
    "depends_on_failed": (replan("s", replan_task(C, replaces=A, dependencies=[A])), "graph"),
    "depends_on_blocked": (replan("s", replan_task(C, replaces=A, dependencies=[COMPARE])), "graph"),
    "unknown_dependency": (replan("s", replan_task(C, replaces=A, dependencies=["ghost"])), "graph"),
    "planner_agent": (replan("s", replan_task(C, replaces=A, agent_type="planner")), "policy"),
    "replanner_agent": (replan("s", replan_task(C, replaces=A, agent_type="replanner")), "policy"),
    "repeats_failed_task": (replan("s", replan_task(C, replaces=A, description=f"do {A}")), "duplicate"),
    # Real runs: the replanner re-created a task that already depended on the failed one.
    "recreates_downstream_task": (replan("s", replan_task(C, replaces=A), replan_task("again", dependencies=[C])), "policy"),
    "unused_side_task": (replan("s", replan_task(C, replaces=A), replan_task("side")), "policy"),
}


@pytest.mark.parametrize("case", sorted(INVALID_REPLANS))
async def test_invalid_replans_are_rejected(case: str) -> None:
    reply, stage = INVALID_REPLANS[case]
    with pytest.raises(ReplanRejectedError) as info:
        await propose(reply)
    assert info.value.stage == stage, info.value.message


async def test_new_tasks_feeding_the_replacement_are_accepted() -> None:
    chain = replan("two steps", replan_task("fetch_c"), replan_task(C, replaces=A, dependencies=["fetch_c"]))
    proposal, _ = await propose(chain)
    assert proposal.replacement_task_id == C and [t.id for t in proposal.tasks] == ["fetch_c", C]


async def test_dangling_task_rejection_explains_the_rule() -> None:
    with pytest.raises(ReplanRejectedError, match=r"\['again'\] do not feed the replacement") as info:
        await propose(replan("s", replan_task(C, replaces=A), replan_task("again", dependencies=[C])))
    assert "do not re-create them" in info.value.message


async def test_task_explosion_is_prevented_by_the_task_limit() -> None:
    many = replan("many", replan_task(C, replaces=A), *(replan_task(f"extra_{i}") for i in range(3)))
    with pytest.raises(ReplanRejectedError, match="plan has 4 tasks; the limit is 3") as info:
        await propose(many, max_new=3)
    assert info.value.stage == "policy"


def test_no_recursive_planner_spawning() -> None:
    registry = AgentRegistry(FakeLLMProvider(), max_tokens=100)
    for agent_type in ("planner", "replanner"):
        with pytest.raises(UnknownAgentTypeError, match="does not execute tasks"):
            registry.resolve(agent_type)
    assert "replanner" not in registry.replanner().output_schema()["$defs"]["ReplanTask"]["properties"]["agent_type"]["enum"]


# --- 18: identical replans ---------------------------------------------------------------------


async def test_identical_replan_is_detected_by_fingerprint() -> None:
    first, _ = await propose(SOURCE_C_REPLAN)
    # Same content, different id and whitespace/case: still the same plan.
    renamed = replan("again", replan_task("source_c_retry", replaces=A, description="  RESEARCH candidate products using source C (web search), since source A is unavailable. "))
    renamed["tasks"][0]["title"] = "Research source c"
    with pytest.raises(ReplanRejectedError) as info:
        await propose(renamed, previous={first.fingerprint})
    assert info.value.stage == "duplicate" and info.value.fingerprint == first.fingerprint

    reworded = replan("new", replan_task(C, replaces=A, description="Use source D instead."))
    other, _ = await propose(reworded, previous={first.fingerprint})
    assert other.fingerprint != first.fingerprint


def test_fingerprint_ignores_ids_and_replaced_task() -> None:
    def plan(first: str, second: str, replaces: str) -> ReplannerOutput:
        t1 = replan_task(first, replaces=replaces, dependencies=[B], description="Search source C.")
        t2 = replan_task(second, dependencies=[first], description="Summarize source C.")
        t1["title"], t2["title"] = "Search", "Summarize"
        return ReplannerOutput.model_validate(replan("s", t1, t2))

    assert plan_fingerprint(plan("x", "y", A).tasks) == plan_fingerprint(plan("p", "q", C).tasks)
    changed = plan("x", "y", A).tasks[0].model_copy(update={"description": "Search source D."})
    assert plan_fingerprint([changed, plan("x", "y", A).tasks[1]]) != plan_fingerprint(plan("x", "y", A).tasks)


# --- 14-17: graph evolution ----------------------------------------------------------------------


def graph_after_failure() -> TaskGraph:
    g = TaskGraph()
    g.add_task(A, status=S.FAILED)
    g.add_task(B, status=S.COMPLETED)
    g.add_task(COMPARE, [A, B])
    return g


def test_replacement_keeps_failed_task_and_unblocks_dependents() -> None:
    g = graph_after_failure()
    assert g.statuses()[COMPARE] is S.BLOCKED

    g.plan_additions([created(C, replaces=A)])  # validation only: nothing changes
    assert C not in g and g.statuses()[COMPARE] is S.BLOCKED

    g.add_task(C, replaces=A)
    assert g.statuses() == {A: S.FAILED, B: S.COMPLETED, C: S.READY, COMPARE: S.PENDING}
    assert g.resolved_statuses()[A] is S.READY  # A presents its replacement's status

    completed_g = TaskGraph()
    completed_g.add_task(A, status=S.FAILED)
    completed_g.add_task(B, status=S.COMPLETED)
    completed_g.add_task(COMPARE, [A, B])
    completed_g.add_task(C, status=S.COMPLETED, replaces=A)
    assert completed_g.statuses()[COMPARE] is S.READY
    assert completed_g.runnable() == [COMPARE]


def test_replacement_chain_resolves_to_the_last_replacement() -> None:
    g = graph_after_failure()
    g.add_task(C, status=S.FAILED, replaces=A)
    assert g.statuses()[COMPARE] is S.BLOCKED  # the replacement failed too
    g.add_task("d", replaces=C)
    assert g.statuses()[COMPARE] is S.PENDING and g.statuses()["d"] is S.READY


@pytest.mark.parametrize(
    ("spec", "error"),
    [
        (created(C, replaces=B), InvalidReplacementError),  # B completed
        (created(C, replaces=COMPARE), InvalidReplacementError),  # blocked, not failed
        (created(C, replaces="ghost"), InvalidReplacementError),
        (created(C, replaces=C), InvalidReplacementError),
        (created(C, A, replaces=A), DependencyCycleError),  # would depend on itself via A
    ],
)
def test_invalid_replacements_are_rejected(spec: TaskCreated, error: type[Exception]) -> None:
    with pytest.raises(error):
        graph_after_failure().plan_additions([spec])


def test_a_failed_task_can_be_replaced_only_once() -> None:
    g = graph_after_failure()
    g.add_task(C, replaces=A)
    with pytest.raises(InvalidReplacementError, match="already has a replacement"):
        g.plan_additions([created("d", replaces=A)])
    with pytest.raises(InvalidReplacementError, match="already has a replacement"):
        graph_after_failure().plan_additions([created(C, replaces=A), created("d", replaces=A)])


def test_projection_links_replacement_and_records_recovery() -> None:
    state = run_state(*base_state_events(), rejected(1), triggered(2), created(C, replaces=A), *completed(C))

    assert state.tasks[A].status is S.FAILED and state.tasks[A].replaced_by == C
    assert state.tasks[A].failure is not None and state.tasks[A].failure.failure_type is F.TOOL_FAILURE
    assert state.tasks[C].replaces == A and state.tasks[COMPARE].status is S.READY
    rec = state.recovery
    assert (rec.replan_count, rec.replan_attempts) == (1, 2)
    assert [(r.replan_number, r.outcome, r.summary) for r in rec.history] == [(1, "rejected", "schema: bad"), (2, "accepted", "s")]

    # The dependent's context carries the replacement's results in place of the failed task.
    context = build_task_context(state, COMPARE)
    assert [(d.task_id, d.replaces) for d in context.dependency_results] == [(C, A), (B, None)]
    assert blocked_tasks(state) == []


@pytest.mark.parametrize(
    "payloads",
    [
        [triggered(1, failed_id=B)],  # not failed
        [triggered(1, failed_id="ghost")],
        [triggered(2)],  # wrong replan number
        [rejected(1), rejected(1)],  # replan number reused
        [triggered(1), created(C, replaces=A), rejected(2)],  # A already replaced
        [ReplanTriggered(reason="r", failed_task_id=A, failure_type=F.TOOL_FAILURE)],  # no number
    ],
)
def test_projector_rejects_inconsistent_recovery_events(payloads: list[Any]) -> None:
    with pytest.raises(InvalidEventError):
        run_state(*base_state_events(), *payloads)


def test_legacy_replan_triggered_is_record_only() -> None:
    state = run_state(*base_state_events(), ReplanTriggered(reason="manual note"))
    assert state.recovery.replan_attempts == 0 and state.recovery.history == ()


def test_blocked_dependents_are_classified_as_dependency_failures() -> None:
    [blocked] = blocked_tasks(base_state())
    assert (blocked.task_id, blocked.failure_type, blocked.blocked_by) == (COMPARE, F.DEPENDENCY_FAILURE, (A,))


def test_failure_record_contains_the_required_fields() -> None:
    state = base_state()
    record = failure_record(state, A)
    assert isinstance(record, FailureRecord)
    assert (record.run_id, record.task_id, record.failure_type, record.error_type, record.message) == (
        state.run_id, A, F.TOOL_FAILURE, "unavailable", "failed",
    )
    assert record.timestamp == state.tasks[A].completed_at


# --- Provider rate limits (HTTP 429) are not task failures ---------------------------------------


def test_rate_limit_is_classified_as_provider_failure() -> None:
    result = TaskExecutionResult.failure("agent:researcher: the LLM provider is rate-limiting requests (HTTP 429)",
                                         error_type="llm_rate_limited")
    classification = classify_result(result)
    assert (classification.failure_type, classification.error_type) == (F.PROVIDER_FAILURE, "llm_rate_limited")


def test_rate_limit_fails_the_run_honestly_without_a_replan() -> None:
    state = base_state(F.PROVIDER_FAILURE, "llm_rate_limited")
    decision = RecoveryPolicy(2).decide(failure_record(state, A), state)
    assert decision.action is RecoveryAction.FAIL_RUN
    assert "LLM provider unavailable" in decision.reason and "Not replanned" in decision.reason
    assert f"task {A!r}" in decision.reason and "llm_rate_limited" in decision.reason
    # Even with no budget left, the reason stays the provider's, not "budget exhausted".
    assert "LLM provider unavailable" in RecoveryPolicy(0).decide(failure_record(state, A), state).reason


@pytest.mark.parametrize(("failure_type", "error_type"), [(F.AGENT_FAILURE, "llm_error"), (F.TOOL_FAILURE, "http_error"),
                                                          (F.TIMEOUT, "agent_timeout"), (F.VALIDATION_FAILURE, "llm_invalid_response")])
def test_other_failures_keep_their_recovery_behaviour(failure_type: FailureType, error_type: str) -> None:
    state = base_state(failure_type, error_type)
    assert RecoveryPolicy(2).decide(failure_record(state, A), state).action is RecoveryAction.REPLAN


# --- Remediation / replacement schema: only the replacement can carry `replaces` ---------------------


def test_replacement_schema_allows_only_the_failed_task_or_null() -> None:
    schema = ReplannerAgent(FakeLLMProvider(), TASK_AGENT_SPECS, max_tokens=10).output_schema(failed_task_id=A)
    assert schema["$defs"]["ReplanTask"]["properties"]["replaces"] == {
        "type": "string", "enum": [A, ""]}


def test_remediation_schema_allows_only_null() -> None:
    schema = ReplannerAgent(FakeLLMProvider(), TASK_AGENT_SPECS, max_tokens=10).output_schema(remediation=True)
    assert schema["$defs"]["ReplanTask"]["properties"]["replaces"] == {"type": "string", "enum": [""]}


def test_empty_replaces_wire_value_normalizes_to_none() -> None:
    task = ReplanTask.model_validate({
        "id": C,
        "title": "Replacement",
        "task_type": "research",
        "agent_type": "researcher",
        "description": "Use a different source to complete the failed research task.",
        "dependencies": [],
        "replaces": "",
    })
    assert task.replaces is None


async def test_replan_request_carries_the_narrowed_schema() -> None:
    _, llm = await propose(SOURCE_C_REPLAN)
    [request] = llm.requests
    assert request.output_schema["$defs"]["ReplanTask"]["properties"]["replaces"] == {
        "type": "string", "enum": [A, ""]}


async def test_null_replaces_on_dependencies_of_the_replacement_is_valid() -> None:
    chain = replan("two steps", replan_task("fetch_c", replaces=""), replan_task(C, replaces=A, dependencies=["fetch_c"]))
    proposal, _ = await propose(chain)
    assert [(t.id, t.replaces) for t in proposal.tasks] == [("fetch_c", None), (C, A)]
