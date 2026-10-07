"""FastAPI dependency providers."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import LLMNotConfiguredError
from app.llm.base import LLMProvider
from app.persistence.database import Database


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


async def get_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    # Open the session here rather than delegating to another async generator: when an
    # endpoint raises, the exception is thrown in at `yield`, and `async with` guarantees
    # the session is closed and its connection returned to the pool.
    async with database.session_factory() as session:
        yield session


def get_llm_provider(request: Request) -> LLMProvider:
    provider: LLMProvider | None = request.app.state.llm_provider
    if provider is None:
        raise LLMNotConfiguredError(
            "no LLM provider is configured (set NEXUS_LLM_PROVIDER and credentials)"
        )
    return provider


SettingsDep = Annotated[Settings, Depends(get_settings)]
DatabaseDep = Annotated[Database, Depends(get_database)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
LLMProviderDep = Annotated[LLMProvider, Depends(get_llm_provider)]
