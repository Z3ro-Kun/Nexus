from uuid import UUID, uuid4

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import RunNotFoundError
from app.models.run import Run, RunStatus
from app.persistence.types import utc_now


class RunRepository:
    """Run rows. Like `EventRepository`, it never commits; the caller owns the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, goal: str) -> Run:
        now = utc_now()
        run = Run(
            id=uuid4(),
            goal=goal,
            status=RunStatus.CREATED.value,
            last_sequence=0,
            created_at=now,
            updated_at=now,
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def get(self, run_id: UUID) -> Run | None:
        return await self._session.get(Run, run_id, populate_existing=True)

    async def set_status(self, run_id: UUID, status: RunStatus) -> None:
        result = await self._session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(status=status.value, updated_at=utc_now())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise RunNotFoundError(f"run {run_id} not found")
