"""Append-only event store.

Sequence assignment
-------------------
Each run row carries a `last_sequence` counter. `append` reserves a block of sequence
numbers with a single statement:

    UPDATE runs SET last_sequence = last_sequence + :n [AND last_sequence = :expected]
    WHERE id = :run_id RETURNING last_sequence

On PostgreSQL the UPDATE takes a row lock on the run that is held until the transaction
ends, so concurrent appenders to the same run are serialized; a waiting writer re-reads
the committed counter before incrementing (READ COMMITTED re-check). On SQLite the whole
database is write-locked for the transaction. Either way the reserved numbers are unique
and, because the counter update and the event inserts commit or roll back together,
gap-free. `UNIQUE(run_id, sequence)` is a second, independent guard.

The repository never commits: the caller owns the transaction, so several appends (and
related run updates) can be committed atomically.
"""

from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import RunNotFoundError, SequenceConflictError
from app.events.base import Event, NewEvent
from app.events.factory import event_from_record
from app.events.types import payload_to_json
from app.models.event import EventRecord
from app.models.run import Run
from app.persistence.types import utc_now


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        run_id: UUID,
        events: Sequence[NewEvent],
        *,
        expected_sequence: int | None = None,
    ) -> list[Event]:
        """Append `events` to the run in order and return them as stored.

        If `expected_sequence` is given, the append only succeeds when the run's last
        sequence still equals it (optimistic concurrency); otherwise it raises
        `SequenceConflictError`.
        """
        if not events:
            raise ValueError("append requires at least one event")

        now = utc_now()
        reserve = (
            update(Run)
            .where(Run.id == run_id)
            .values(last_sequence=Run.last_sequence + len(events), updated_at=now)
            .returning(Run.last_sequence)
            .execution_options(synchronize_session=False)
        )
        if expected_sequence is not None:
            reserve = reserve.where(Run.last_sequence == expected_sequence)

        new_last = (await self._session.execute(reserve)).scalar_one_or_none()
        if new_last is None:
            await self._raise_reservation_failure(run_id, expected_sequence)
        assert new_last is not None

        first = new_last - len(events) + 1
        records = [
            EventRecord(
                id=uuid4(),
                run_id=run_id,
                sequence=first + offset,
                event_type=draft.event_type.value,
                timestamp=now,
                agent_id=draft.agent_id,
                task_id=draft.task_id,
                payload=payload_to_json(draft.payload),
            )
            for offset, draft in enumerate(events)
        ]
        self._session.add_all(records)
        await self._session.flush()
        return [event_from_record(record) for record in records]

    async def list_for_run(self, run_id: UUID, *, after_sequence: int = 0) -> list[Event]:
        """Events of one run with `sequence > after_sequence`, in sequence order."""
        stmt = (
            select(EventRecord)
            .where(EventRecord.run_id == run_id, EventRecord.sequence > after_sequence)
            .order_by(EventRecord.sequence)
        )
        records = (await self._session.scalars(stmt)).all()
        return [event_from_record(record) for record in records]

    async def _raise_reservation_failure(
        self, run_id: UUID, expected_sequence: int | None
    ) -> None:
        current = await self._session.scalar(select(Run.last_sequence).where(Run.id == run_id))
        if current is None:
            raise RunNotFoundError(f"run {run_id} not found")
        raise SequenceConflictError(
            f"run {run_id} is at sequence {current}, expected {expected_sequence}"
        )
