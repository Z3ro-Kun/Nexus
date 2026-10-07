"""Scheduler behavior on real databases (SQLite always; PostgreSQL when configured).

Concurrency is demonstrated with synchronization primitives rather than timing: tasks
block on an asyncio.Event, and the test observes which tasks are executing at the same
time before releasing them. If the scheduler ran tasks sequentially, the second task
would never start while the first is blocked, and `wait_until_started` would time out.
"""

import asyncio
import time
from uuid import UUID

import pytest

from tests.helpers import complete_run
from app.core.exceptions import InvalidEventError, RunNotActiveError, SequenceConflictError
from app.events.base import Event, NewEvent
from app.events.types import EventType, TaskCreated, TaskStarted
from app.orchestration.scheduler import ScheduleReport, Scheduler
from app.orchestration.task_executor import (
    Outcome,
    Script,
    ScriptedTaskExecutor,
    TaskExecutionResult,
)
from app.persistence.database import Database
from app.persistence.repositories import EventRepository
from app.services.runs import RunService
from app.state.models import TaskState, TaskStatus
from app.state.projector import apply, project

S = TaskStatus
TIMEOUT = 10  # seconds; only reached if the behavior under test is broken

DIAMOND = {"t1": [], "t2": ["t1"], "t3": ["t1"], "t4": ["t2", "t3"]}


async def create_graph(database: Database, edges: dict[str, list[str]]) -> UUID:
    async with database.session_factory() as session:
        service = RunService(session)
        run = await service.create_run("scheduler test")
        await service.create_tasks(
            run.id,
            [TaskCreated(task_id=t, title=t, dependencies=deps) for t, deps in edges.items()],
        )
        return run.id


async def load_events(database: Database, run_id: UUID) -> list[Event]:
    async with database.session_factory() as session:
        return await EventRepository(session).list_for_run(run_id)


def seq(events: list[Event], event_type: EventType, task_id: str) -> int:
    """Sequence of the single event of this type for this task (asserts it is unique)."""
    matches = [
        e.sequence
        for e in events
        if e.event_type is event_type and getattr(e.payload, "task_id", None) == task_id
    ]
    assert len(matches) == 1, f"expected one {event_type.value} for {task_id}, got {matches}"
    return matches[0]


async def schedule(database: Database, run_id: UUID, executor: ScriptedTaskExecutor) -> ScheduleReport:
    return await asyncio.wait_for(Scheduler(database, executor).run(run_id), TIMEOUT)


# --- Graph scenarios -----------------------------------------------------------------------


async def test_a_independent_tasks_run_concurrently(database: Database) -> None:
    run_id = await create_graph(database, {"t1": [], "t2": [], "t3": []})
    gate = asyncio.Event()
    executor = ScriptedTaskExecutor(default=Script(wait_for=gate))
    scheduling = asyncio.create_task(Scheduler(database, executor).run(run_id))

    for task_id in ("t1", "t2", "t3"):
        await asyncio.wait_for(executor.wait_until_started(task_id), TIMEOUT)
    assert executor.active == {"t1", "t2", "t3"}  # all three executing at once
    gate.set()
    report = await asyncio.wait_for(scheduling, TIMEOUT)

    assert sorted(report.started) == ["t1", "t2", "t3"]
    assert report.task_statuses == {"t1": S.COMPLETED, "t2": S.COMPLETED, "t3": S.COMPLETED}


async def test_b_linear_chain_runs_in_order(database: Database) -> None:
    run_id = await create_graph(database, {"t1": [], "t2": ["t1"], "t3": ["t2"]})
    executor = ScriptedTaskExecutor()

    report = await schedule(database, run_id, executor)

    assert executor.start_order == ["t1", "t2", "t3"]
    assert executor.max_active == 1
    events = await load_events(database, run_id)
    assert seq(events, EventType.TASK_COMPLETED, "t1") < seq(events, EventType.TASK_STARTED, "t2")
    assert seq(events, EventType.TASK_COMPLETED, "t2") < seq(events, EventType.TASK_STARTED, "t3")
    assert set(report.task_statuses.values()) == {S.COMPLETED}


async def test_c_diamond_respects_dependencies_and_parallelizes_branches(database: Database) -> None:
    run_id = await create_graph(database, DIAMOND)
    gate = asyncio.Event()
    executor = ScriptedTaskExecutor({"t2": Script(wait_for=gate), "t3": Script(wait_for=gate)})
    scheduling = asyncio.create_task(Scheduler(database, executor).run(run_id))

    await asyncio.wait_for(executor.wait_until_started("t2"), TIMEOUT)
    await asyncio.wait_for(executor.wait_until_started("t3"), TIMEOUT)
    assert executor.active == {"t2", "t3"}
    assert executor.calls["t4"] == 0  # join has not started while a branch is running
    gate.set()
    report = await asyncio.wait_for(scheduling, TIMEOUT)

    events = await load_events(database, run_id)
    t1_done = seq(events, EventType.TASK_COMPLETED, "t1")
    assert t1_done < seq(events, EventType.TASK_STARTED, "t2")
    assert t1_done < seq(events, EventType.TASK_STARTED, "t3")
    t4_start = seq(events, EventType.TASK_STARTED, "t4")
    assert seq(events, EventType.TASK_COMPLETED, "t2") < t4_start
    assert seq(events, EventType.TASK_COMPLETED, "t3") < t4_start
    assert set(report.task_statuses.values()) == {S.COMPLETED}


async def test_join_waits_for_both_concurrent_inputs(database: Database) -> None:
    """T1, T2 -> T3: T1 and T2 execute at the same time; T3 only after both finish."""
    run_id = await create_graph(database, {"t1": [], "t2": [], "t3": ["t1", "t2"]})
    release_t1, release_t2 = asyncio.Event(), asyncio.Event()
    executor = ScriptedTaskExecutor(
        {"t1": Script(wait_for=release_t1), "t2": Script(wait_for=release_t2)}
    )
    scheduling = asyncio.create_task(Scheduler(database, executor).run(run_id))

    await asyncio.wait_for(executor.wait_until_started("t1"), TIMEOUT)
    await asyncio.wait_for(executor.wait_until_started("t2"), TIMEOUT)
    assert executor.active == {"t1", "t2"}

    release_t1.set()
    async with database.session_factory() as session:  # wait until t1 is recorded
        for _ in range(500):
            if (await RunService(session).get_task(run_id, "t1")).status is S.COMPLETED:
                break
            await asyncio.sleep(0.01)
    assert executor.calls["t3"] == 0  # t2 still running, so t3 must not start

    release_t2.set()
    await asyncio.wait_for(scheduling, TIMEOUT)
    assert executor.calls == {"t1": 1, "t2": 1, "t3": 1}
    assert executor.start_order[-1] == "t3"


async def test_concurrent_execution_is_faster_than_sequential(database: Database) -> None:
    """Supporting timing evidence (the primitives-based tests above are the main proof)."""
    delay = 0.5
    run_id = await create_graph(database, {"t1": [], "t2": [], "t3": ["t1", "t2"]})
    executor = ScriptedTaskExecutor(
        {"t1": Script(delay_seconds=delay), "t2": Script(delay_seconds=delay)}
    )

    started = time.perf_counter()
    await schedule(database, run_id, executor)
    elapsed = time.perf_counter() - started

    assert executor.max_active == 2
    assert elapsed < 2 * delay, f"took {elapsed:.2f}s; sequential would be >= {2 * delay}s"


# --- Failure -------------------------------------------------------------------------------


async def test_e_failed_dependency_prevents_execution(database: Database) -> None:
    run_id = await create_graph(database, {"t1": [], "t2": ["t1"]})
    executor = ScriptedTaskExecutor({"t1": Script(outcome=Outcome.FAILURE)})

    report = await schedule(database, run_id, executor)

    assert executor.calls == {"t1": 1}
    assert (report.started, report.completed, report.failed) == (["t1"], [], ["t1"])
    assert report.task_statuses == {"t1": S.FAILED, "t2": S.BLOCKED}
    events = await load_events(database, run_id)
    assert seq(events, EventType.TASK_FAILED, "t1")
    assert not [e for e in events if getattr(e.payload, "task_id", None) == "t2" and e.event_type is not EventType.TASK_CREATED]


async def test_failed_branch_blocks_join_but_other_branch_completes(database: Database) -> None:
    run_id = await create_graph(database, DIAMOND)
    executor = ScriptedTaskExecutor({"t2": Script(outcome=Outcome.FAILURE)})

    report = await schedule(database, run_id, executor)

    assert report.task_statuses == {"t1": S.COMPLETED, "t2": S.FAILED, "t3": S.COMPLETED, "t4": S.BLOCKED}
    assert executor.calls["t4"] == 0


async def test_executor_exception_is_recorded_as_task_failure(database: Database) -> None:
    class Broken:
        async def execute(self, task: TaskState, context: object) -> TaskExecutionResult:
            raise RuntimeError("executor crashed")

    run_id = await create_graph(database, {"t1": []})

    report = await asyncio.wait_for(Scheduler(database, Broken()).run(run_id), TIMEOUT)

    assert report.task_statuses == {"t1": S.FAILED}
    async with database.session_factory() as session:
        task = await RunService(session).get_task(run_id, "t1")
    assert task.error == "executor raised RuntimeError: executor crashed"


# --- Duplicate execution -------------------------------------------------------------------


async def test_f_competing_schedulers_execute_each_task_once(database: Database) -> None:
    edges = {"a": [], "b": [], "c": [], "d": ["a", "b"], "e": ["c"], "f": ["d", "e"]}
    run_id = await create_graph(database, edges)
    executor = ScriptedTaskExecutor()  # shared, so it counts executions across schedulers

    reports = await asyncio.wait_for(
        asyncio.gather(*(Scheduler(database, executor).run(run_id) for _ in range(4))), TIMEOUT
    )

    assert executor.calls == {task_id: 1 for task_id in edges}
    started = [task_id for report in reports for task_id in report.started]
    assert sorted(started) == sorted(edges)  # each task claimed by exactly one scheduler
    events = await load_events(database, run_id)
    for task_id in edges:
        seq(events, EventType.TASK_STARTED, task_id)  # asserts exactly one TaskStarted
    assert project(events).tasks.keys() == edges.keys()
    assert {t.status for t in project(events).tasks.values()} == {S.COMPLETED}


async def test_concurrent_claims_of_one_task_admit_exactly_one(database: Database) -> None:
    """The database-backed invariant: at most one TaskStarted per task, whoever writes it.

    Every claimant reads the same READY state before any of them writes. A losing claim is
    rejected either with SequenceConflictError (the log moved past the sequence it read)
    or, if it re-reads, with InvalidEventError (the task is already RUNNING).
    """
    claimants = 10
    run_id = await create_graph(database, {"t1": []})
    have_read = 0
    all_read = asyncio.Event()

    async def claim() -> bool:
        nonlocal have_read
        async with database.session_factory() as session:
            service = RunService(session)
            state = await service.get_state(run_id)
            assert state.tasks["t1"].status is S.READY
            have_read += 1
            if have_read == claimants:
                all_read.set()
            await all_read.wait()
            try:
                await service.append_events(
                    run_id,
                    [NewEvent(payload=TaskStarted(task_id="t1"))],
                    expected_sequence=state.last_sequence,
                )
            except (SequenceConflictError, InvalidEventError):
                return False
            return True

    results = await asyncio.wait_for(
        asyncio.gather(*(claim() for _ in range(claimants))), TIMEOUT
    )

    assert results.count(True) == 1
    events = await load_events(database, run_id)
    assert seq(events, EventType.TASK_STARTED, "t1")  # exactly one

    # Even with a fresh read and no expected sequence, a second start is rejected.
    async with database.session_factory() as session:
        with pytest.raises(InvalidEventError):
            await RunService(session).append_events(run_id, [NewEvent(payload=TaskStarted(task_id="t1"))])


async def test_rescheduling_a_finished_graph_executes_nothing(database: Database) -> None:
    run_id = await create_graph(database, {"t1": [], "t2": ["t1"]})
    executor = ScriptedTaskExecutor()
    await schedule(database, run_id, executor)

    again = await schedule(database, run_id, executor)

    assert again.started == []
    assert executor.calls == {"t1": 1, "t2": 1}


async def test_completed_run_cannot_be_scheduled(database: Database) -> None:
    run_id = await create_graph(database, {"t1": []})
    await schedule(database, run_id, ScriptedTaskExecutor())
    async with database.session_factory() as session:
        await complete_run(RunService(session), run_id)  # Phase 8: completion needs verified work

    with pytest.raises(RunNotActiveError):
        await schedule(database, run_id, ScriptedTaskExecutor())


# --- Reconstruction ------------------------------------------------------------------------


async def test_g_execution_state_is_reconstructable_from_events(database: Database) -> None:
    run_id = await create_graph(database, {**DIAMOND, "t5": ["t4"], "t6": []})
    executor = ScriptedTaskExecutor({"t5": Script(outcome=Outcome.FAILURE)})
    report = await schedule(database, run_id, executor)

    # Rebuild from nothing but the stored events, in a fresh session.
    events = await load_events(database, run_id)
    rebuilt = project(events)
    incremental = None
    for event in events:
        incremental = apply(incremental, event)

    assert rebuilt == project(events) == incremental
    assert {t: task.status for t, task in rebuilt.tasks.items()} == report.task_statuses
    assert report.task_statuses == {
        "t1": S.COMPLETED, "t2": S.COMPLETED, "t3": S.COMPLETED,
        "t4": S.COMPLETED, "t5": S.FAILED, "t6": S.COMPLETED,
    }
    for task in rebuilt.tasks.values():
        assert task.started_at is not None and task.completed_at is not None
        assert task.created_at <= task.started_at <= task.completed_at
    assert rebuilt.tasks["t5"].error == "scripted failure of task 't5'"
    async with database.session_factory() as session:
        assert await RunService(session).get_state(run_id) == rebuilt
