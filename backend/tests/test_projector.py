from uuid import uuid4

import pytest

from app.core.exceptions import InvalidEventError, UnsupportedEventError
from app.events.types import (
    ApprovalRequested,
    ConflictDetected,
    ConflictResolved,
    EventPayload,
    EventType,
    FactAdded,
    ReplanTriggered,
    RunCompleted,
    RunCreated,
    RunFailed,
    TaskCompleted,
    TaskCreated,
    TaskStarted,
    ToolCalled,
    ToolFailed,
    ToolSucceeded,
    VerificationStarted,
)
from app.models.run import RunStatus
from app.state.models import ConflictStatus, TaskStatus
from app.state.projections import HANDLERS
from app.state.projector import apply, project
from tests.helpers import history, verified_completion

RUN_ID = uuid4()


def run(*payloads: EventPayload):  # type: ignore[no-untyped-def]
    return project(history(RUN_ID, RunCreated(goal="Plan a trip"), *payloads))


# --- one test per meaningful event type --------------------------------------------------


def test_run_created_produces_initial_state() -> None:
    state = run()

    assert state.run_id == RUN_ID
    assert state.goal == "Plan a trip"
    assert state.status is RunStatus.CREATED
    assert state.tasks == {} and state.facts == {} and state.conflicts == {}
    assert state.last_sequence == 1


def test_task_created_adds_task() -> None:
    state = run(TaskCreated(task_id="t1", title="Find flights", description="cheapest"))

    task = state.tasks["t1"]
    # No dependencies, so the new task is immediately READY (derived, see test_task_graph).
    assert (task.title, task.description, task.status) == ("Find flights", "cheapest", TaskStatus.READY)
    assert task.created_at is not None and task.started_at is None
    assert state.last_sequence == 2


def test_fact_added_records_fact_with_provenance() -> None:
    events = history(RUN_ID, RunCreated(goal="g"), FactAdded(fact_id="f1", content="x", source="web"))
    events[1] = events[1].model_copy(update={"agent_id": "researcher"})

    fact = project(events).facts["f1"]
    assert (fact.content, fact.source, fact.agent_id, fact.sequence) == ("x", "web", "researcher", 2)


def test_conflict_detected_records_open_conflict() -> None:
    state = run(
        FactAdded(fact_id="f1", content="flight at 9:00"),
        FactAdded(fact_id="f2", content="flight at 10:00"),
        ConflictDetected(conflict_id="c1", description="times differ", fact_ids=["f1", "f2"]),
    )

    conflict = state.conflicts["c1"]
    assert conflict.status is ConflictStatus.OPEN
    assert conflict.fact_ids == ("f1", "f2")


def test_conflict_resolved_updates_conflict() -> None:
    state = run(
        FactAdded(fact_id="f1", content="a"),
        ConflictDetected(conflict_id="c1", description="d", fact_ids=["f1"]),
        ConflictResolved(conflict_id="c1", resolution="f1 confirmed"),
    )

    conflict = state.conflicts["c1"]
    assert (conflict.status, conflict.resolution) == (ConflictStatus.RESOLVED, "f1 confirmed")


def test_task_completed_updates_task() -> None:
    state = run(
        TaskCreated(task_id="t1", title="Find flights"),
        TaskStarted(task_id="t1"),
        TaskCompleted(task_id="t1", summary="found 3"),
    )

    assert state.tasks["t1"].status is TaskStatus.COMPLETED
    assert state.tasks["t1"].summary == "found 3"


def test_run_completed_updates_status() -> None:
    # Phase 8: a run completes only after its work completed and verification passed.
    base = history(RUN_ID, RunCreated(goal="Plan a trip"), TaskCreated(task_id="t1", title="x"),
                   TaskStarted(task_id="t1"), TaskCompleted(task_id="t1"))
    state = project(base + verified_completion(base, summary="done"))

    assert (state.status, state.completion_summary) == (RunStatus.COMPLETED, "done")


def test_run_cannot_complete_without_verified_work() -> None:
    with pytest.raises(InvalidEventError, match="no verification checkpoint"):
        run(RunCompleted(summary="done"))
    with pytest.raises(InvalidEventError, match="task t1 is ready"):
        run(TaskCreated(task_id="t1", title="x"), RunCompleted())


def test_events_after_completion_are_rejected() -> None:
    base = history(RUN_ID, RunCreated(goal="g"), TaskCreated(task_id="t1", title="x"), TaskStarted(task_id="t1"), TaskCompleted(task_id="t1"))
    completed = base + verified_completion(base)
    late = history(RUN_ID, FactAdded(fact_id="f", content="late"), start=len(completed) + 1)
    with pytest.raises(InvalidEventError):
        project(completed + late)


def test_run_failed_updates_status() -> None:
    state = run(RunFailed(reason="no flights"))

    assert (state.status, state.failure_reason) == (RunStatus.FAILED, "no flights")


# --- later-phase events are recorded without inventing semantics ------------------------


def test_later_phase_events_only_advance_sequence() -> None:
    base = run(TaskCreated(task_id="t1", title="x"))
    # Tool events became meaningful in Phase 4 (see test_tool_events_track_tool_calls).
    after = run(
        TaskCreated(task_id="t1", title="x"),
        ReplanTriggered(reason="r"),
        VerificationStarted(verification_id="v1"),
        ApprovalRequested(approval_id="a1", description="ok?"),
    )

    assert after.model_dump(exclude={"last_sequence", "updated_at"}) == base.model_dump(
        exclude={"last_sequence", "updated_at"}
    )
    assert after.last_sequence == 5


def test_tool_events_track_tool_calls() -> None:
    state = run(
        ToolCalled(tool_call_id="c1", tool_name="calculator", arguments={"expression": "1+1"}),
        ToolSucceeded(tool_call_id="c1", result={"result": 2}, metadata={"duration_ms": 1}),
        ToolCalled(tool_call_id="c2", tool_name="http_fetch", arguments={"url": "http://10.0.0.1"}),
        ToolFailed(tool_call_id="c2", error="blocked", error_type="ssrf_blocked"),
    )

    c1, c2 = state.tool_calls["c1"], state.tool_calls["c2"]
    assert (c1.status.value, c1.output) == ("succeeded", {"result": 2})
    assert (c2.status.value, c2.error_type) == ("failed", "ssrf_blocked")


@pytest.mark.parametrize(
    "payloads",
    [
        [ToolSucceeded(tool_call_id="missing")],
        [ToolCalled(tool_call_id="c1", tool_name="x"), ToolCalled(tool_call_id="c1", tool_name="x")],
        [ToolCalled(tool_call_id="c1", tool_name="x"), ToolFailed(tool_call_id="c1", error="e"), ToolSucceeded(tool_call_id="c1")],
    ],
    ids=["result-without-call", "duplicate-call", "closed-twice"],
)
def test_invalid_tool_event_sequences_are_rejected(payloads: list[EventPayload]) -> None:
    with pytest.raises(InvalidEventError):
        run(*payloads)


def test_every_event_type_is_handled() -> None:
    assert set(HANDLERS) | {EventType.RUN_CREATED} == set(EventType)


def test_event_without_handler_is_rejected_not_ignored() -> None:
    events = history(RUN_ID, RunCreated(goal="g"), FactAdded(fact_id="f", content="x"))
    state = project(events[:1])
    handlers = {k: v for k, v in HANDLERS.items() if k is not EventType.FACT_ADDED}

    with pytest.raises(UnsupportedEventError):
        apply(state, events[1], handlers)


# --- rule violations ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "payloads",
    [
        [TaskCreated(task_id="t1", title="a"), TaskCreated(task_id="t1", title="b")],
        [TaskCompleted(task_id="missing")],
        [
            TaskCreated(task_id="t1", title="a"),
            TaskStarted(task_id="t1"),
            TaskCompleted(task_id="t1"),
            TaskCompleted(task_id="t1"),
        ],
        [FactAdded(fact_id="f", content="a"), FactAdded(fact_id="f", content="b")],
        [ConflictDetected(conflict_id="c", description="d", fact_ids=["missing"])],
        [ConflictResolved(conflict_id="missing", resolution="r")],
        [RunCreated(goal="again")],
        [RunFailed(reason="x"), RunCompleted()],
    ],
    ids=[
        "duplicate-task",
        "complete-unknown-task",
        "complete-task-twice",
        "duplicate-fact",
        "conflict-unknown-fact",
        "resolve-unknown-conflict",
        "second-run-created",
        "event-after-failed",
    ],
)
def test_invalid_histories_are_rejected(payloads: list[EventPayload]) -> None:
    with pytest.raises(InvalidEventError):
        run(*payloads)


def test_history_must_start_with_run_created() -> None:
    with pytest.raises(InvalidEventError):
        project(history(RUN_ID, FactAdded(fact_id="f", content="x")))
    with pytest.raises(InvalidEventError):
        project([])


def test_sequences_must_be_contiguous() -> None:
    events = history(RUN_ID, RunCreated(goal="g"), FactAdded(fact_id="f", content="x"))
    gap = events[1].model_copy(update={"sequence": 3})

    with pytest.raises(InvalidEventError):
        project([events[0], gap])
    with pytest.raises(InvalidEventError):
        project(list(reversed(events)))


def test_events_from_another_run_are_rejected() -> None:
    events = history(RUN_ID, RunCreated(goal="g"))
    foreign = history(uuid4(), RunCreated(goal="g"), FactAdded(fact_id="f", content="x"))[1]

    with pytest.raises(InvalidEventError):
        project([events[0], foreign])


# --- reconstruction ----------------------------------------------------------------------

_WORK_HISTORY = history(
    RUN_ID,
    RunCreated(goal="g"),
    TaskCreated(task_id="t1", title="a"),
    TaskCreated(task_id="t2", title="b"),
    FactAdded(fact_id="f1", content="x"),
    FactAdded(fact_id="f2", content="not x"),
    ConflictDetected(conflict_id="c1", description="x vs not x", fact_ids=["f1", "f2"]),
    ToolCalled(tool_call_id="call", tool_name="lookup", arguments={"q": "x"}),
    ConflictResolved(conflict_id="c1", resolution="x"),
    TaskStarted(task_id="t1"),
    TaskStarted(task_id="t2"),
    TaskCompleted(task_id="t1"),
    TaskCompleted(task_id="t2", summary="ok"),
)
FULL_HISTORY = _WORK_HISTORY + verified_completion(_WORK_HISTORY, summary="all done")


def test_reconstruction_is_deterministic() -> None:
    first = project(FULL_HISTORY)
    second = project(FULL_HISTORY)
    from_copies = project([event.model_copy() for event in FULL_HISTORY])

    assert first == second == from_copies
    assert first.model_dump_json() == second.model_dump_json()


def test_incremental_projection_equals_full_projection() -> None:
    state = project(FULL_HISTORY[:4])
    for event in FULL_HISTORY[4:]:
        state = apply(state, event)

    assert state == project(FULL_HISTORY)


def test_reconstructed_state_matches_history() -> None:
    state = project(FULL_HISTORY)

    assert state.status is RunStatus.COMPLETED
    assert {t.status for t in state.tasks.values()} == {TaskStatus.COMPLETED}
    assert list(state.facts) == ["f1", "f2"]
    assert state.conflicts["c1"].status is ConflictStatus.RESOLVED
    assert state.last_sequence == len(FULL_HISTORY)
    assert state.updated_at == FULL_HISTORY[-1].timestamp
