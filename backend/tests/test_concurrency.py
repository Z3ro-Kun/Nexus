"""Concurrent writers to the same run.

These run on SQLite (database-level write lock) and, when NEXUS_TEST_DATABASE_URL is set,
on PostgreSQL (row lock on the run). Both use separate sessions/connections per writer.
"""

import asyncio
from uuid import UUID

from app.core.exceptions import SequenceConflictError
from app.events.base import Event
from app.events.types import FactAdded
from app.persistence.database import Database
from app.persistence.repositories import EventRepository
from app.services.runs import RunService
from tests.helpers import drafts

WRITERS = 12


async def _create_run(database: Database) -> UUID:
    async with database.session_factory() as session:
        return (await RunService(session).create_run("concurrency")).id


async def test_concurrent_appends_get_unique_contiguous_sequences(database: Database) -> None:
    run_id = await _create_run(database)
    start = asyncio.Event()

    async def writer(n: int) -> list[Event]:
        async with database.session_factory() as session:
            await start.wait()
            stored = await EventRepository(session).append(
                run_id,
                drafts(
                    FactAdded(fact_id=f"w{n}-a", content="a"),
                    FactAdded(fact_id=f"w{n}-b", content="b"),
                ),
            )
            await asyncio.sleep(0)  # yield while holding the reservation
            await session.commit()
            return stored

    tasks = [asyncio.create_task(writer(n)) for n in range(WRITERS)]
    start.set()
    results = await asyncio.gather(*tasks)

    # Each writer's own batch is contiguous.
    for batch in results:
        assert batch[1].sequence == batch[0].sequence + 1

    async with database.session_factory() as session:
        service = RunService(session)
        events = await service.get_events(run_id)
        state = await service.get_state(run_id)
        run = await service.get_run(run_id)

    expected = list(range(1, 2 * WRITERS + 2))
    assert [e.sequence for e in events] == expected
    assert run.last_sequence == expected[-1]
    assert len(state.facts) == 2 * WRITERS
    assert sorted(e.sequence for batch in results for e in batch) == expected[1:]


async def test_concurrent_validated_appends_never_corrupt_history(database: Database) -> None:
    """Optimistic writers either commit on the state they validated or get a conflict."""
    run_id = await _create_run(database)
    start = asyncio.Event()

    async def writer(n: int) -> bool:
        async with database.session_factory() as session:
            await start.wait()
            try:
                await RunService(session).append_events(
                    run_id, drafts(FactAdded(fact_id=f"f{n}", content=str(n)))
                )
            except SequenceConflictError:
                return False
            return True

    tasks = [asyncio.create_task(writer(n)) for n in range(WRITERS)]
    start.set()
    outcomes = await asyncio.gather(*tasks)

    async with database.session_factory() as session:
        service = RunService(session)
        events = await service.get_events(run_id)
        state = await service.get_state(run_id)

    committed = sum(outcomes)
    assert committed >= 1
    assert [e.sequence for e in events] == list(range(1, committed + 2))
    assert len(state.facts) == committed
