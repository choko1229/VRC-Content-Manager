"""Global exception handlers: a safety net, not the primary error-handling path.

Most routes already catch specific AppError subclasses themselves so they can
render a friendly, in-context HTML error (see app/web/pages/items.py). These
handlers exist so that any route that forgets to -- or any genuinely
unexpected exception -- still returns a clean response instead of leaking a
raw traceback to the client, while the full traceback is always logged
server-side.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.exceptions import AppError, ConflictError, DriveError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def handle_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def handle_validation(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def handle_conflict(request: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(DriveError)
    async def handle_drive_error(request: Request, exc: DriveError) -> JSONResponse:
        logger.error("unhandled DriveError on %s: %s", request.url.path, exc, exc_info=True)
        return JSONResponse(status_code=502, content={"detail": "Google Driveとの通信でエラーが発生しました。"})

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning("unhandled AppError on %s: %s", request.url.path, exc)
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled exception on %s: %s", request.url.path, exc, exc_info=True)
        return JSONResponse(status_code=500, content={"detail": "予期しないエラーが発生しました。"})
