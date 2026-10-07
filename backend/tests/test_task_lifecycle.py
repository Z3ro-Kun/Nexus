"""Projector-level task lifecycle rules. These hold for every writer, not only the scheduler."""

from uuid import uuid4

import pytest

from app.core.exceptions import InvalidEventError
from app.events.factory import parse_payload
from app.events.types import (
    EventPayload,
    RunCreated,
    TaskCancelled,
    TaskCompleted,
    TaskCreated,
    TaskFailed,
    TaskStarted,
)
from app.state.models import RunState, TaskStatus
from app.state.projector import project
from tests.helpers import history

S = TaskStatus


def run(*payloads: EventPayload) -> RunState:
    return project(history(uuid4(), RunCreated(goal="g"), *payloads))


def task(task_id: str, *deps: str) -> TaskCreated:
    return TaskCreated(task_id=task_id, title=task_id, dependencies=list(deps))


def test_full_lifecycle_sets_status_and_timestamps() -> None:
    state = run(task("t1"), task("t2", "t1"), TaskStarted(task_id="t1"), TaskCompleted(task_id="t1", summary="ok"))

    t1, t2 = state.tasks["t1"], state.tasks["t2"]
    assert (t1.status, t1.summary) == (S.COMPLETED, "ok")
    assert t1.created_at < t1.started_at < t1.completed_at  # type: ignore[operator]
    assert t2.status is S.READY  # derived from its completed dependency


def test_failure_records_error_and_blocks_dependents() -> None:
    state = run(task("t1"), task("t2", "t1"), task("t3", "t2"), TaskStarted(task_id="t1"), TaskFailed(task_id="t1", error="boom"))

    assert (state.tasks["t1"].status, state.tasks["t1"].error) == (S.FAILED, "boom")
    assert state.tasks["t1"].completed_at is not None
    assert state.tasks["t2"].status is S.BLOCKED
    assert state.tasks["t3"].status is S.BLOCKED


def test_cancelling_a_not_started_task_blocks_dependents() -> None:
    state = run(task("t1"), task("t2", "t1"), TaskCancelled(task_id="t1", reason="not needed"))

    assert state.tasks["t1"].status is S.CANCELLED
    assert state.tasks["t2"].status is S.BLOCKED


@pytest.mark.parametrize(
    "payloads",
    [
        [task("t1"), TaskStarted(task_id="t1"), TaskStarted(task_id="t1")],
        [task("t1"), TaskStarted(task_id="t1"), TaskCompleted(task_id="t1"), TaskStarted(task_id="t1")],
        [task("t1"), task("t2", "t1"), TaskStarted(task_id="t2")],
        [task("t1"), TaskStarted(task_id="missing")],
        [task("t1"), TaskCompleted(task_id="t1")],
        [task("t1"), TaskFailed(task_id="t1", error="x")],
        [task("t1"), TaskStarted(task_id="t1"), TaskCancelled(task_id="t1")],
        [task("t1"), TaskStarted(task_id="t1"), TaskFailed(task_id="t1", error="x"), TaskCompleted(task_id="t1")],
    ],
    ids=[
        "start-twice",
        "restart-completed",
        "start-before-dependency-completes",
        "start-unknown",
        "complete-without-start",
        "fail-without-start",
        "cancel-running",
        "complete-after-failure",
    ],
)
def test_illegal_transitions_are_rejected(payloads: list[EventPayload]) -> None:
    with pytest.raises(InvalidEventError):
        run(*payloads)


@pytest.mark.parametrize(
    "payloads",
    [
        [task("t1"), task("t1")],
        [task("t1", "t1")],
        [task("t2", "t1")],
        [task("t1"), TaskStarted(task_id="t1"), TaskFailed(task_id="t1", error="x"), task("t2", "t1")],
        [TaskCreated(task_id="c", title="c", parent_id="nope")],
    ],
    ids=["duplicate", "self-dependency", "missing-dependency", "depends-on-failed", "unknown-parent"],
)
def test_invalid_task_creation_is_rejected_by_the_projector(payloads: list[EventPayload]) -> None:
    with pytest.raises(InvalidEventError):
        run(*payloads)


def test_phase_1_task_created_payloads_remain_valid() -> None:
    payload = parse_payload("TaskCreated", {"task_id": "t1", "title": "old", "description": None})

    state = run(payload)

    assert state.tasks["t1"].dependencies == ()
    assert state.tasks["t1"].task_type is None
    assert state.tasks["t1"].status is S.READY
