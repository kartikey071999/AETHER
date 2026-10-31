"""Application factory; serving the API does not start agents or construct LLM providers."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from aether.api.routers import admin, reads
from aether.api.services import ApiServices
from aether.config import AetherSettings, load_settings
from aether.models.settings import ProviderSettings
from aether.storage.database import Database

_logger = logging.getLogger("aether.api")


def create_app(
    settings: AetherSettings | None = None,
    *,
    database: Database | None = None,
    provider_settings: ProviderSettings | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    settings = settings if settings is not None else load_settings()
    owns_database = database is None
    database = database if database is not None else Database.from_settings(settings)
    api = ApiServices(
        settings,
        database,
        provider_settings if provider_settings is not None else ProviderSettings(),
        clock=clock,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owns_database:
                await database.close()

    app = FastAPI(title="AETHER Local API", version="0.1.0", lifespan=lifespan)
    app.state.services = api
    app.include_router(reads)
    app.include_router(admin)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, error: RequestValidationError) -> JSONResponse:
        # FastAPI's default error includes raw input and can echo a credential in a bad body.
        return api.response(
            {"detail": [{"loc": item["loc"], "type": item["type"]} for item in error.errors()]},
            status_code=422,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        response = api.response({"detail": error.detail}, status_code=error.status_code)
        if error.headers:
            response.headers.update(error.headers)
        return response

    @app.exception_handler(LookupError)
    async def not_found(request: Request, error: LookupError) -> JSONResponse:
        return api.response({"detail": "Record does not exist"}, status_code=404)

    @app.exception_handler(PermissionError)
    async def forbidden(request: Request, error: PermissionError) -> JSONResponse:
        return api.response(
            {"detail": "Operation is not permitted for this audience"}, status_code=403
        )

    @app.exception_handler(ValueError)
    async def conflict(request: Request, error: ValueError) -> JSONResponse:
        return api.response(
            {"detail": "Operation conflicts with persistent state"}, status_code=409
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError) -> JSONResponse:
        _logger.error("API database operation failed", extra={"error_type": type(error).__name__})
        return api.response({"detail": "Database is unavailable"}, status_code=503)

    return app
