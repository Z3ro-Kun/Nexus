import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.models  # noqa: F401 - registers tables on Base.metadata
from app.core.config import Settings, get_settings
from app.main import create_app
from app.persistence.database import Base, Database

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

# Set NEXUS_TEST_DATABASE_URL (environment, or the repo-root .env) to e.g.
# postgresql+asyncpg://USER:PASSWORD@localhost:5432/nexus_test to also run every database
# test against PostgreSQL. The database's tables are DROPPED and recreated.
_DOTENV = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
# The process environment wins, so `NEXUS_TEST_DATABASE_URL= uv run pytest` runs SQLite only.
POSTGRES_TEST_URL = (
    os.environ["NEXUS_TEST_DATABASE_URL"]
    if "NEXUS_TEST_DATABASE_URL" in os.environ
    else _DOTENV.get("NEXUS_TEST_DATABASE_URL")
) or None

if POSTGRES_TEST_URL:
    _production_db = os.getenv("POSTGRES_DB") or _DOTENV.get("POSTGRES_DB") or "nexus"
    if (make_url(POSTGRES_TEST_URL).database or "") == _production_db:
        raise pytest.UsageError(
            f"NEXUS_TEST_DATABASE_URL points at the application database {_production_db!r}; "
            "tests drop all tables. Use a separate database such as nexus_test."
        )


@pytest.fixture
def settings() -> Settings:
    # _env_file=None keeps a developer's local .env from leaking into tests.
    return Settings(_env_file=None, NEXUS_ENVIRONMENT="test")  # type: ignore[call-arg]


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as test_client:  # context manager runs the lifespan
        yield test_client


@pytest.fixture(params=["sqlite", "postgresql"])
async def database(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Database]:
    """A fresh, empty schema on each supported backend."""
    if request.param == "sqlite":
        # A file (not :memory:) so that concurrent sessions share one database.
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 'nexus.db'}")
    else:
        if not POSTGRES_TEST_URL:
            pytest.skip("set NEXUS_TEST_DATABASE_URL to run against PostgreSQL")
        db = Database(POSTGRES_TEST_URL)

    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
async def api(settings: Settings, database: Database) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, database=database)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
