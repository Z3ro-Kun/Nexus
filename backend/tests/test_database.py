import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.persistence.database import Base, Database


def test_database_module_imports() -> None:
    assert Base.metadata is not None
    assert Database is not None


def test_engine_targets_asyncpg_without_connecting(settings: Settings) -> None:
    # Constructing the engine must not require a running PostgreSQL.
    database = Database(settings.database_url)

    assert database.engine.url.drivername == "postgresql+asyncpg"
    assert database.engine.url.host == settings.postgres_host


async def test_session_and_ping_against_in_memory_sqlite() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    try:
        assert await database.ping() is True

        async for session in database.session():
            assert isinstance(session, AsyncSession)
            result = await session.execute(text("SELECT 41 + 1"))
            assert result.scalar_one() == 42
    finally:
        await database.dispose()


@pytest.mark.skipif(
    os.getenv("NEXUS_RUN_DB_TESTS") != "1",
    reason="set NEXUS_RUN_DB_TESTS=1 with PostgreSQL running (docker compose up -d)",
)
async def test_ping_against_configured_postgres() -> None:
    database = Database(Settings().database_url)
    try:
        assert await database.ping() is True
    finally:
        await database.dispose()
