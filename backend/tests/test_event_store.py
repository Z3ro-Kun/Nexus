from datetime import timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.exceptions import AppendOnlyViolationError, RunNotFoundError, SequenceConflictError
from app.events.types import EventType, FactAdded, RunCreated, TaskCreated
from app.models.event import EventRecord
from app.models.run import Run
from app.persistence.database import Database
from app.persistence.repositories import EventRepository, RunRepository
from app.persistence.types import utc_now
from tests.helpers import drafts


async def _new_run(database: Database, goal: str = "test goal") -> UUID:
    async with database.session_factory() as session:
        run = await RunRepository(session).create(goal)
        await session.commit()
        return run.id


async def _last_sequence(database: Database, run_id: UUID) -> int:
    async with database.session_factory() as session:
        value = await session.scalar(select(Run.last_sequence).where(Run.id == run_id))
        assert value is not None
        return value


async def test_append_single_event(database: Database) -> None:
    run_id = await _new_run(database)

    async with database.session_factory() as session:
        [stored] = await EventRepository(session).append(
            run_id, drafts(RunCreated(goal="g"), agent_id="planner")
        )
        await session.commit()

    assert stored.run_id == run_id
    assert stored.sequence == 1
    assert stored.event_type is EventType.RUN_CREATED
    assert stored.agent_id == "planner"
    assert stored.timestamp.tzinfo is not None

    async with database.session_factory() as session:
        [loaded] = await EventRepository(session).list_for_run(run_id)
    assert loaded == stored
    assert loaded.timestamp.utcoffset() == timezone.utc.utcoffset(None)


async def test_multiple_appends_get_consecutive_sequences_in_order(database: Database) -> None:
    run_id = await _new_run(database)

    async with database.session_factory() as session:
        repo = EventRepository(session)
        first = await repo.append(run_id, drafts(RunCreated(goal="g")))
        batch = await repo.append(
            run_id,
            drafts(
                TaskCreated(task_id="t1", title="one"),
                TaskCreated(task_id="t2", title="two"),
                TaskCreated(task_id="t3", title="three"),
            ),
        )
        await session.commit()

    assert [e.sequence for e in first + batch] == [1, 2, 3, 4]
    assert [e.payload.task_id for e in batch] == ["t1", "t2", "t3"]  # type: ignore[attr-defined]
    assert await _last_sequence(database, run_id) == 4


async def test_events_are_retrieved_in_sequence_order(database: Database) -> None:
    run_id = await _new_run(database)
    async with database.session_factory() as session:
        repo = EventRepository(session)
        for i in range(1, 8):
            await repo.append(run_id, drafts(FactAdded(fact_id=f"f{i}", content=str(i))))
        await session.commit()

    async with database.session_factory() as session:
        repo = EventRepository(session)
        events = await repo.list_for_run(run_id)
        tail = await repo.list_for_run(run_id, after_sequence=5)

    assert [e.sequence for e in events] == list(range(1, 8))
    assert [e.payload.fact_id for e in events] == [f"f{i}" for i in range(1, 8)]  # type: ignore[attr-defined]
    assert [e.sequence for e in tail] == [6, 7]


async def test_events_cannot_be_modified_through_the_orm(database: Database) -> None:
    run_id = await _new_run(database)
    async with database.session_factory() as session:
        await EventRepository(session).append(run_id, drafts(RunCreated(goal="g")))
        await session.commit()

    async with database.session_factory() as session:
        record = await session.scalar(select(EventRecord).where(EventRecord.run_id == run_id))
        assert record is not None
        record.event_type = "Tampered"
        with pytest.raises(AppendOnlyViolationError):
            await session.flush()

    async with database.session_factory() as session:
        record = await session.scalar(select(EventRecord).where(EventRecord.run_id == run_id))
        assert record is not None
        await session.delete(record)
        with pytest.raises(AppendOnlyViolationError):
            await session.flush()


@pytest.mark.parametrize(
    "statement",
    ["UPDATE events SET event_type = 'Tampered'", "DELETE FROM events"],
)
async def test_events_cannot_be_modified_with_raw_sql(database: Database, statement: str) -> None:
    run_id = await _new_run(database)
    async with database.session_factory() as session:
        await EventRepository(session).append(run_id, drafts(RunCreated(goal="g")))
        await session.commit()

    async with database.session_factory() as session:
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(text(statement))

    async with database.session_factory() as session:
        [event] = await EventRepository(session).list_for_run(run_id)
    assert event.event_type is EventType.RUN_CREATED


def test_repository_exposes_no_mutation_methods() -> None:
    public = {name for name in dir(EventRepository) if not name.startswith("_")}
    assert public == {"append", "list_for_run"}


async def test_runs_are_isolated(database: Database) -> None:
    run_a = await _new_run(database, "a")
    run_b = await _new_run(database, "b")

    async with database.session_factory() as session:
        repo = EventRepository(session)
        await repo.append(run_a, drafts(RunCreated(goal="a"), FactAdded(fact_id="a1", content="a")))
        await repo.append(run_b, drafts(RunCreated(goal="b")))
        await repo.append(run_a, drafts(FactAdded(fact_id="a2", content="a")))
        await session.commit()

    async with database.session_factory() as session:
        repo = EventRepository(session)
        events_a = await repo.list_for_run(run_a)
        events_b = await repo.list_for_run(run_b)

    assert [e.sequence for e in events_a] == [1, 2, 3]
    assert {e.run_id for e in events_a} == {run_a}
    assert [e.sequence for e in events_b] == [1]
    assert events_b[0].payload == RunCreated(goal="b")


async def test_duplicate_run_sequence_is_rejected_by_the_database(database: Database) -> None:
    run_id = await _new_run(database)
    async with database.session_factory() as session:
        await EventRepository(session).append(run_id, drafts(RunCreated(goal="g")))
        await session.commit()

    async with database.session_factory() as session:
        session.add(
            EventRecord(
                id=uuid4(),
                run_id=run_id,
                sequence=1,
                event_type="RunCreated",
                timestamp=utc_now(),
                payload={"goal": "duplicate"},
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_multi_event_append_is_atomic(database: Database) -> None:
    run_id = await _new_run(database)
    # Plant a row at sequence 2 behind the counter's back so the batch below collides.
    async with database.session_factory() as session:
        session.add(
            EventRecord(
                id=uuid4(),
                run_id=run_id,
                sequence=2,
                event_type="FactAdded",
                timestamp=utc_now(),
                payload={"fact_id": "planted", "content": "x"},
            )
        )
        await session.commit()

    async with database.session_factory() as session:
        with pytest.raises(IntegrityError):
            await EventRepository(session).append(
                run_id,
                drafts(
                    RunCreated(goal="g"),
                    FactAdded(fact_id="f1", content="collides at 2"),
                    FactAdded(fact_id="f2", content="x"),
                ),
            )
        await session.rollback()

    async with database.session_factory() as session:
        events = await EventRepository(session).list_for_run(run_id)
    assert [(e.sequence, e.payload.fact_id) for e in events] == [(2, "planted")]  # type: ignore[attr-defined]
    assert await _last_sequence(database, run_id) == 0  # counter rolled back too


async def test_uncommitted_append_leaves_no_trace(database: Database) -> None:
    run_id = await _new_run(database)
    async with database.session_factory() as session:
        await EventRepository(session).append(run_id, drafts(RunCreated(goal="g")))
        await session.rollback()

    async with database.session_factory() as session:
        assert await EventRepository(session).list_for_run(run_id) == []
    assert await _last_sequence(database, run_id) == 0


async def test_append_to_unknown_run_fails(database: Database) -> None:
    async with database.session_factory() as session:
        with pytest.raises(RunNotFoundError):
            await EventRepository(session).append(uuid4(), drafts(RunCreated(goal="g")))


async def test_append_with_stale_expected_sequence_conflicts(database: Database) -> None:
    run_id = await _new_run(database)
    async with database.session_factory() as session:
        repo = EventRepository(session)
        await repo.append(run_id, drafts(RunCreated(goal="g")), expected_sequence=0)
        await session.commit()

        with pytest.raises(SequenceConflictError):
            await repo.append(
                run_id, drafts(FactAdded(fact_id="f", content="x")), expected_sequence=0
            )
        await session.rollback()

    assert await _last_sequence(database, run_id) == 1


async def test_append_requires_events(database: Database) -> None:
    run_id = await _new_run(database)
    async with database.session_factory() as session:
        with pytest.raises(ValueError):
            await EventRepository(session).append(run_id, [])
