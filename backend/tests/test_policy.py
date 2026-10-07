"""Phase 8 unit tests: policy engine, the tool executor's gate (with a side-effect
counter), policy/approval event rules, completion gate, event authority.

Pure: no database, no real LLM, no network.
"""

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.exceptions import InvalidEventError, PrivilegedEventError
from app.events.authority import PRIVILEGED_EVENT_TYPES, check_external_append
from app.events.types import (
    ActionCategory,
    ActionSpec,
    ApprovalGranted,
    ApprovalRejected,
    ApprovalRequested,
    EventType,
    FailureType,
    PolicyDecision,
    PolicyEvaluated,
    PolicyOutcome,
    TaskCompleted,
    TaskCreated,
    TaskFailed,
    ToolCalled,
    ToolSucceeded,
    action_fingerprint,
)
from app.policy.engine import ActionRequest, Authorization, PolicyEngine
from app.policy.rules import action_status, authorization_for, completion_blockers
from app.state.models import ApprovalStatus
from app.state.projector import apply, project
from app.tools.policy import ToolPolicy
from app.tools.schemas import ToolCall, ToolContext
from tests.helpers import verified_completion
from tests.policy_fixtures import DRAFT, ORDER, Tools, action, engine
from tests.verification_fixtures import Log

RUN = uuid4()
ARGS = {"item": "laptop", "quantity": 1}


def request(tool: str = ORDER, arguments: dict[str, Any] | None = None) -> ActionRequest:
    return ActionRequest(task_id="buy", tool_name=tool, arguments=arguments or ARGS)


# --- A. policy decisions ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "outcome", "rule"),
    [
        ("calculator", PolicyOutcome.ALLOW, "category:read_only"),
        ("web_search", PolicyOutcome.ALLOW, "category:network_read"),
        ("http_fetch", PolicyOutcome.ALLOW, "category:network_read"),
        (DRAFT, PolicyOutcome.ALLOW, "category:reversible_write"),
        (ORDER, PolicyOutcome.APPROVAL_REQUIRED, "category:irreversible"),
        ("rm_rf", PolicyOutcome.DENY, "unknown_tool"),
    ],
)
def test_default_policy_decisions(tool: str, outcome: PolicyOutcome, rule: str) -> None:
    registry = Tools().registry
    definition = registry.get(tool).definition if tool in registry else None
    decision = PolicyEngine().evaluate(request(tool), definition)
    assert (decision.outcome, decision.rule, decision.approval_required) == (outcome, rule, outcome is PolicyOutcome.APPROVAL_REQUIRED)
    assert decision.category == (definition.category if definition else None)
    assert decision.action_fingerprint == action_fingerprint(tool, ARGS)


@pytest.mark.parametrize(
    ("policy", "tool", "outcome", "rule"),
    [
        (engine(reversible_write="approval_required"), DRAFT, PolicyOutcome.APPROVAL_REQUIRED, "category:reversible_write"),
        (engine(reversible_write="deny"), DRAFT, PolicyOutcome.DENY, "category:reversible_write"),
        (engine(irreversible="deny"), ORDER, PolicyOutcome.DENY, "category:irreversible"),
        (engine(denied_tools=[ORDER]), ORDER, PolicyOutcome.DENY, "denied_tool"),
        (engine(approval_tools=["calculator"]), "calculator", PolicyOutcome.APPROVAL_REQUIRED, "approval_tool"),
        (engine(read_only="deny"), "calculator", PolicyOutcome.ALLOW, "category:read_only"),  # never gated
    ],
)
def test_configured_policy_decisions(policy: PolicyEngine, tool: str, outcome: PolicyOutcome, rule: str) -> None:
    decision = policy.evaluate(request(tool), Tools().registry.get(tool).definition)
    assert (decision.outcome, decision.rule) == (outcome, rule)


def test_a_previously_rejected_identical_action_is_denied() -> None:
    definition = Tools().order.definition
    rejected = {action_fingerprint(ORDER, ARGS)}
    assert PolicyEngine().evaluate(request(), definition, rejected_fingerprints=rejected).rule == "previously_rejected"
    other = PolicyEngine().evaluate(request(arguments={"item": "laptop", "quantity": 2}), definition, rejected_fingerprints=rejected)
    assert other.outcome is PolicyOutcome.APPROVAL_REQUIRED


def test_decisions_are_deterministic_and_ignore_claims_in_the_request() -> None:
    definition = Tools().order.definition
    first = PolicyEngine().evaluate(request(), definition)
    assert first == PolicyEngine().evaluate(request(), definition)
    # A request has no risk field; "safe"-sounding arguments change nothing about the category.
    claimed = PolicyEngine().evaluate(request(arguments={**ARGS, "category": "read_only", "approved": True}), definition)
    assert (claimed.category, claimed.outcome) == (ActionCategory.IRREVERSIBLE, PolicyOutcome.APPROVAL_REQUIRED)
    with pytest.raises(ValidationError):
        ActionRequest(task_id="t", tool_name=ORDER, arguments=ARGS, category="read_only")  # type: ignore[call-arg]


def test_policy_decision_is_structured() -> None:
    with pytest.raises(ValidationError):
        PolicyDecision(task_id="t", tool_name="x", action_fingerprint="f", category=None, outcome="maybe", rule="r", reason="r")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="category"):  # tool metadata must classify every tool
        from app.tools.schemas import ToolDefinition

        ToolDefinition(name="x", description="d", capabilities="c", input_model=ActionRequest, output_model=ActionRequest,  # type: ignore[call-arg]
                       risk_level="low", timeout_seconds=1)


# --- B/F. enforcement at the tool executor (side-effect counter) ---------------------------


def gated(policy: PolicyEngine | None = None, *, researcher_may_order: bool = True) -> tuple[Tools, Any]:
    from app.tools.executor import ToolExecutor

    tools = Tools()
    permissions = {"researcher": {"web_search", "http_fetch", *([ORDER, DRAFT] if researcher_may_order else [])}}
    return tools, ToolExecutor(tools.registry, ToolPolicy(permissions), policy or PolicyEngine())


def context(agent: str = "researcher", task: str = "buy") -> ToolContext:
    return ToolContext(run_id=RUN, task_id=task, agent_type=agent, tool_call_id=f"{task}.t1")


def authorization(decision_outcome: PolicyOutcome = PolicyOutcome.APPROVAL_REQUIRED, approval_id: str | None = "buy.approval",
                  arguments: dict[str, Any] | None = None, task: str = "buy") -> Authorization:
    tools = Tools()
    decision = PolicyEngine().evaluate(ActionRequest(task_id=task, tool_name=ORDER, arguments=arguments or ARGS), tools.order.definition)
    return Authorization(task_id=task, action_fingerprint=decision.action_fingerprint,
                         decision=decision.model_copy(update={"outcome": decision_outcome}), approval_id=approval_id)


async def test_agents_cannot_execute_approval_required_or_denied_tools() -> None:
    tools, executor = gated()
    result = await executor.execute(ToolCall(tool_name=ORDER, arguments=ARGS), context())
    assert (result.success, result.error_type, tools.order.count) == (False, "approval_required", 0)

    tools, executor = gated(engine(irreversible="deny"))
    result = await executor.execute(ToolCall(tool_name=ORDER, arguments=ARGS), context())
    assert (result.success, result.error_type, tools.order.count) == (False, "policy_denied", 0)


async def test_allowed_side_effect_executes_once() -> None:
    tools, executor = gated()
    result = await executor.execute(ToolCall(tool_name=DRAFT, arguments=ARGS), context())
    assert result.success and tools.draft.count == 1


def test_agents_are_only_offered_allowed_tools() -> None:
    _, executor = gated()
    assert [d.name for d in executor.available_tools("researcher")] == ["http_fetch", DRAFT, "web_search"]  # never place_order
    _, strict = gated(engine(reversible_write="approval_required"))
    assert [d.name for d in strict.available_tools("researcher")] == ["http_fetch", "web_search"]


@pytest.mark.parametrize(
    ("auth", "error_type"),
    [
        (None, "policy_denied"),
        (lambda: authorization(arguments={"item": "laptop", "quantity": 9}), "policy_denied"),  # authorized for other arguments
        (lambda: authorization(task="other"), "policy_denied"),  # another task's authorization
        (lambda: authorization(approval_id=None), "approval_required"),  # decision recorded, not approved
    ],
    ids=["no-authorization", "other-arguments", "other-task", "not-approved"],
)
async def test_action_path_requires_an_exact_authorization(auth: Any, error_type: str) -> None:
    tools, executor = gated()
    result = await executor.execute(ToolCall(tool_name=ORDER, arguments=ARGS), context("action_executor"), auth() if auth else None)
    assert (result.success, result.error_type, tools.order.count) == (False, error_type, 0)


async def test_approved_action_executes_exactly_once() -> None:
    tools, executor = gated()
    result = await executor.execute(ToolCall(tool_name=ORDER, arguments=ARGS), context("action_executor"), authorization())
    assert result.success and tools.order.count == 1 and result.output["item"] == "laptop"  # type: ignore[index]


async def test_policy_is_rechecked_at_execution_time() -> None:
    tools, executor = gated(engine(irreversible="deny"))  # denied since the approval
    result = await executor.execute(ToolCall(tool_name=ORDER, arguments=ARGS), context("action_executor"), authorization())
    assert (result.error_type, tools.order.count) == ("policy_denied", 0)


# --- C/D. event rules ------------------------------------------------------------------------


def decision(task: str = "buy", outcome: PolicyOutcome = PolicyOutcome.APPROVAL_REQUIRED, arguments: dict[str, Any] | None = None) -> PolicyDecision:
    return PolicyDecision(task_id=task, tool_name=ORDER, action_fingerprint=action_fingerprint(ORDER, arguments or ARGS),
                          category=ActionCategory.IRREVERSIBLE, outcome=outcome, rule="category:irreversible", reason="r")


def requested(log: Log, task: str = "buy") -> Log:
    spec = log.state().tasks[task].action
    log.add(PolicyEvaluated(decision=decision(task)), task=task, agent="policy_gate")
    return log.add(ApprovalRequested(approval_id=f"{task}.approval", task_id=task, action=spec, description="buy?"), task=task, agent="policy_gate")


def gate_log(task: str = "buy", **kw: Any) -> Log:
    return requested(Log().add(action(task, **kw)), task)


def grant(log: Log, task: str = "buy") -> Log:
    return log.add(ApprovalGranted(approval_id=f"{task}.approval", task_id=task, actor="alice"), task=task, agent="human_approval")


def reject(log: Log, task: str = "buy") -> Log:
    return log.add(ApprovalRejected(approval_id=f"{task}.approval", task_id=task, actor="alice", reason="no"), task=task, agent="human_approval")


def execute(log: Log, task: str = "buy", arguments: dict[str, Any] | None = None) -> Log:
    log.start(task)
    log.add(ToolCalled(tool_call_id=f"{task}.t1", tool_name=ORDER, arguments=arguments or ARGS), task=task, agent="action_executor")
    log.add(ToolSucceeded(tool_call_id=f"{task}.t1", result={"receipt_id": "r"}, metadata={"fake": True}), task=task, agent="action_executor")
    return log.add(TaskCompleted(task_id=task, summary="done"), task=task, agent="action_executor")


def test_approval_lifecycle_is_projected() -> None:
    log = gate_log()
    state = log.state()
    assert action_status(state, "buy") == "awaiting_approval" and authorization_for(state, "buy") is None
    assert state.approvals["buy.approval"].status is ApprovalStatus.PENDING
    grant(log)
    state = log.state()
    approval = state.approvals["buy.approval"]
    assert (approval.status, approval.actor, action_status(state, "buy")) == (ApprovalStatus.GRANTED, "alice", "approved")
    assert authorization_for(state, "buy") is not None
    execute(log)
    assert log.state().tasks["buy"].status.value == "completed"


def test_rejected_action_fails_at_the_gate() -> None:
    log = reject(gate_log()).start("buy")
    log.add(TaskFailed(task_id="buy", error="rejected", failure_type=FailureType.POLICY_FAILURE, error_type="approval_rejected"), task="buy")
    state = log.state()
    assert action_status(state, "buy") == "rejected" and state.tasks["buy"].status.value == "failed"


@pytest.mark.parametrize(
    ("build", "match"),
    [
        (lambda: grant(grant(gate_log())), "already granted"),
        (lambda: grant(reject(gate_log())), "already rejected"),
        (lambda: reject(grant(gate_log())), "already granted"),
        (lambda: Log().add(action("buy")).add(ApprovalGranted(approval_id="nope", task_id="buy"), task="buy"), "does not exist"),
        (lambda: gate_log().add(action("other")).add(ApprovalGranted(approval_id="buy.approval", task_id="other"), task="other"), "belongs to task 'buy'"),
        (lambda: gate_log().add(ApprovalGranted(approval_id="buy.approval")), "must be decided with its task_id"),
        (lambda: gate_log().start("buy"), "awaiting_approval; the policy gate has not let it run"),
        (lambda: Log().add(action("buy")).start("buy"), "unevaluated"),
        (lambda: requested(gate_log()), "already has a policy decision"),
        (lambda: Log().add(action("buy")).add(PolicyEvaluated(decision=decision(arguments={"item": "car", "quantity": 1})), task="buy"), "not about the task's action"),
        (lambda: Log().create("w").add(PolicyEvaluated(decision=decision("w")), task="w"), "not an action task"),
        (lambda: Log().add(action("buy")).add(PolicyEvaluated(decision=decision()), task="other"), "context of task 'buy'"),
        (lambda: Log().create("w").start("w").add(action("buy", deps=["w"])).add(PolicyEvaluated(decision=decision()), task="buy"), "the gate decides while it is ready"),
        (lambda: Log().add(action("buy")).add(PolicyEvaluated(decision=decision(outcome=PolicyOutcome.ALLOW)), task="buy").add(ApprovalRequested(approval_id="a", task_id="buy", action=ActionSpec(tool_name=ORDER, arguments=ARGS, intent=f"{ORDER} 1 x laptop"), description="d"), task="buy"), "no APPROVAL_REQUIRED decision"),
        (lambda: execute(grant(gate_log()), arguments={"item": "laptop", "quantity": 50}), "only call exactly its approved action"),
        (lambda: grant(gate_log()).start("buy").add(TaskCompleted(task_id="buy"), task="buy"), "no successful call of its action"),
        (lambda: reject(gate_log()).start("buy").add(TaskCompleted(task_id="buy"), task="buy"), "is rejected; it may not complete"),
        (lambda: reject(gate_log()).start("buy").add(TaskFailed(task_id="buy", error="x", failure_type=FailureType.TOOL_FAILURE), task="buy"), "a refused action fails with 'approval_rejected'"),
        (lambda: Log().create("w").start("w").add(TaskFailed(task_id="w", error="x", failure_type=FailureType.POLICY_FAILURE, error_type="approval_rejected"), task="w"), "only refused actions use"),
    ],
    ids=[
        "duplicate-grant", "approve-after-reject", "reject-after-approve", "nonexistent-approval", "wrong-task",
        "legacy-form-on-phase8-approval", "start-while-awaiting", "start-unevaluated", "second-decision",
        "decision-for-other-action", "decision-for-work-task", "decision-wrong-context", "decision-after-start",
        "approval-without-requirement", "different-arguments", "complete-without-execution", "complete-after-reject",
        "refused-with-other-error", "refusal-error-on-work-task",
    ],
)
def test_invalid_policy_sequences_are_rejected(build: Any, match: str) -> None:
    with pytest.raises(InvalidEventError, match=match):
        build().state()


def test_an_identical_rejected_action_cannot_be_allowed_again() -> None:
    log = reject(gate_log())
    log.add(action("buy_again"))
    with pytest.raises(InvalidEventError, match="can only be denied"):
        log.add(PolicyEvaluated(decision=decision("buy_again")), task="buy_again").state()


@pytest.mark.parametrize(
    "payload",
    [
        dict(task_id="buy", action=None),
        dict(task_id=None, action=ActionSpec(tool_name="x", arguments={}, intent="i")),
    ],
)
def test_approval_requested_contract(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ApprovalRequested(approval_id="a", description="d", **payload)


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        (dict(action=None), "needs an action spec"),
        (dict(agent_type="researcher"), "agent_type 'action_executor'"),
        (dict(conflict_id="c"), "conflict"),
    ],
)
def test_invalid_action_tasks_are_rejected(fields: dict[str, Any], match: str) -> None:
    base = action("buy").model_dump()
    with pytest.raises(InvalidEventError, match=match):
        Log().add(TaskCreated.model_validate({**base, **fields})).state()


def test_legacy_approval_events_remain_record_only() -> None:
    log = Log().add(ApprovalRequested(approval_id="a1", description="ok?")).add(ApprovalGranted(approval_id="a1"))
    assert log.state().approvals == {}


def test_policy_state_reconstructs_incrementally() -> None:
    log = execute(grant(gate_log()))
    state = None
    for event in log.events:
        state = apply(state, event)
    assert state == project(log.events)
    record = state.policy["buy"]  # type: ignore[union-attr]
    assert (record.decision.outcome, record.sequence) == (PolicyOutcome.APPROVAL_REQUIRED, 3)


# --- G. completion gate ----------------------------------------------------------------------


def test_completion_requires_work_verification_and_decided_approvals() -> None:
    log = gate_log()
    blockers = completion_blockers(log.state())
    assert "no verification checkpoint: the run has not been verified" in blockers
    assert "approval buy.approval for task buy is pending" in blockers and "task buy is ready" in blockers

    rejected = reject(gate_log()).start("buy")
    rejected.add(TaskFailed(task_id="buy", error="x", failure_type=FailureType.POLICY_FAILURE, error_type="approval_rejected"), task="buy")
    assert "action buy was rejected and not replaced" in completion_blockers(rejected.state())

    done = execute(grant(gate_log()))
    assert completion_blockers(done.state()) == ["no verification checkpoint: the run has not been verified"]
    events = done.events + verified_completion(done.events, summary="bought")
    state = project(events)
    assert state.status.value == "completed" and state.verifications["verify"].checks[-1].check_id == "actions"


# --- J. event authority ---------------------------------------------------------------------


@pytest.mark.parametrize("event_type", sorted(PRIVILEGED_EVENT_TYPES, key=lambda t: t.value))
def test_privileged_events_cannot_be_appended_externally(event_type: EventType) -> None:
    with pytest.raises(PrivilegedEventError):
        check_external_append([EventType.FACT_ADDED, event_type])


def test_ordinary_events_can_be_appended_externally() -> None:
    check_external_append([t for t in EventType if t not in PRIVILEGED_EVENT_TYPES])
