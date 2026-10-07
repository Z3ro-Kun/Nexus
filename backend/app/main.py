
"""FastAPI application factory and ASGI entrypoint."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.llm.base import LLMProvider
from app.llm.provider import build_provider
from app.persistence.database import Database
from app.tools.factory import build_artifact_workspace, build_tool_registry, workspace_of
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    llm_provider: LLMProvider | None = None,
    tool_registry: ToolRegistry | None = None,
) -> FastAPI:
    """Build the app. `database`, `llm_provider` and `tool_registry` override the
    configured ones (tests)."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_database = database is None
        if owns_database:
            app.state.database = Database(settings.database_url)
        logger.info("%s API starting (%s)", settings.app_name, settings.environment)
        try:
            yield
        finally:
            if owns_database:
                await app.state.database.dispose()

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    if database is not None:
        app.state.database = database
    app.state.llm_provider = llm_provider if llm_provider is not None else build_provider(settings)
    app.state.tool_registry = (
        tool_registry if tool_registry is not None else build_tool_registry(settings, build_artifact_workspace(settings))
    )
    # Phase 10: the controlled artifact workspace (behind the artifact_write tool), used by
    # run completion and the download API. None when artifacts are disabled.
    app.state.artifact_workspace = workspace_of(app.state.tool_registry)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
