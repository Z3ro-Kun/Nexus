from uuid import uuid4

import pytest

from app.core.exceptions import InvalidEventError, RunNotFoundError, SequenceConflictError
from app.events.base import NewEvent
from app.events.types import EventType, FactAdded, RunCompleted, RunFailed, TaskCompleted, TaskCreated, TaskStarted
from app.models.run import RunStatus
from app.persistence.database import Database
from app.services.runs import RunService
from tests.helpers import complete_run, drafts


async def test_create_run_writes_run_created_event(database: Database) -> None:
    async with database.session_factory() as session:
        run = await RunService(session).create_run("Plan a trip")

    async with database.session_factory() as session:
        service = RunService(session)
        events = await service.get_events(run.id)
        state = await service.get_state(run.id)

    assert run.status == RunStatus.CREATED.value
    assert run.last_sequence == 1
    assert [(e.sequence, e.event_type) for e in events] == [(1, EventType.RUN_CREATED)]
    assert state.goal == "Plan a trip"


async def test_invalid_event_is_rejected_before_it_is_stored(database: Database) -> None:
    async with database.session_factory() as session:
        run = await RunService(session).create_run("g")

    async with database.session_factory() as session:
        with pytest.raises(InvalidEventError):
            await RunService(session).append_events(
                run.id,
                drafts(FactAdded(fact_id="f1", content="ok"), TaskCompleted(task_id="missing")),
            )

    async with database.session_factory() as session:
        service = RunService(session)
        assert [e.sequence for e in await service.get_events(run.id)] == [1]
        assert (await service.get_run(run.id)).last_sequence == 1


@pytest.mark.parametrize(
    ("terminal", "status"),
    [(RunCompleted(summary="done"), RunStatus.COMPLETED), (RunFailed(reason="x"), RunStatus.FAILED)],
)
async def test_run_status_follows_terminal_events(
    database: Database, terminal: RunCompleted | RunFailed, status: RunStatus
) -> None:
    async with database.session_factory() as session:
        run = await RunService(session).create_run("g")
        if isinstance(terminal, RunCompleted):
            # Phase 8: RunCompleted needs completed, verified work first.
            await RunService(session).append_events(run.id, drafts(TaskCreated(task_id="t1", title="x")))
            await RunService(session).append_events(run.id, [NewEvent(payload=TaskStarted(task_id="t1"), task_id="t1"),
                                                             NewEvent(payload=TaskCompleted(task_id="t1"), task_id="t1")])
            await complete_run(RunService(session), run.id, summary=terminal.summary)
        else:
            await RunService(session).append_events(run.id, drafts(terminal))

    async with database.session_factory() as session:
        service = RunService(session)
        stored = await service.get_run(run.id)
        state = await service.get_state(run.id)

    assert stored.status == status.value
    assert state.status is status


async def test_expected_sequence_mismatch_conflicts(database: Database) -> None:
    async with database.session_factory() as session:
        run = await RunService(session).create_run("g")

    async with database.session_factory() as session:
        with pytest.raises(SequenceConflictError):
            await RunService(session).append_events(
                run.id, drafts(FactAdded(fact_id="f", content="x")), expected_sequence=0
            )


async def test_unknown_run(database: Database) -> None:
    async with database.session_factory() as session:
        service = RunService(session)
        with pytest.raises(RunNotFoundError):
            await service.get_state(uuid4())
        with pytest.raises(RunNotFoundError):
            await service.append_events(uuid4(), drafts(FactAdded(fact_id="f", content="x")))
