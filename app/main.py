from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api.routers import downloads as downloads_router
from app.api.routers import oauth as oauth_router
from app.config import get_instance_config, get_settings
from app.core.error_handlers import register_exception_handlers
from app.db.migrate import run_migrations
from app.db.session import get_sessionmaker
from app.logging_conf import configure_logging
from app.services import drive_sync_service
from app.web.fragments import items as items_fragments
from app.web.fragments import settings as settings_fragments
from app.web.fragments import shops as shops_fragments
from app.web.pages import auth as auth_pages
from app.web.pages import avatars as avatars_pages
from app.web.pages import items as items_pages
from app.web.pages import settings as settings_pages
from app.web.pages import shops as shops_pages

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Always reachable, regardless of setup/auth state.
_PUBLIC_PREFIXES = ("/static", "/healthz")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.upload_tmp_dir.mkdir(parents=True, exist_ok=True)

    if drive_sync_service.needs_setup():
        logger.warning(
            "no local database found at %s; visit /setup to connect Google Drive", settings.local_db_path
        )
    else:
        run_migrations()
        try:
            session_local = get_sessionmaker()
            with session_local() as db:
                await asyncio.to_thread(drive_sync_service.check_remote_drift, db)
        except Exception:
            logger.exception("startup Drive drift check failed (continuing with local copy)")

    stop_event = asyncio.Event()
    sync_task = asyncio.create_task(drive_sync_service.sync_loop(stop_event))
    logger.info("startup complete (data_dir=%s)", settings.data_dir)

    yield

    stop_event.set()
    try:
        await asyncio.wait_for(sync_task, timeout=10)
    except asyncio.TimeoutError:
        logger.warning("sync loop did not stop within timeout; cancelling")
        sync_task.cancel()

    if drive_sync_service.is_dirty():
        try:
            await asyncio.wait_for(drive_sync_service.flush_now_async(), timeout=20)
        except asyncio.TimeoutError:
            logger.warning("shutdown Drive sync flush timed out; some recent changes may not be synced")
    logger.info("shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="VRC Content Manager", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    register_exception_handlers(app)

    # Registered before SessionMiddleware below on purpose: Starlette's
    # add_middleware() prepends to the middleware list, so the middleware
    # added *last* ends up outermost (runs first per-request). This gate
    # reads request.session, so SessionMiddleware must run before it --
    # i.e. SessionMiddleware must be added after this one.
    @app.middleware("http")
    async def security_gate(request: Request, call_next):
        path = request.url.path
        if path == "/" or any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES):
            return await call_next(request)

        # First run: no DB exists yet, so there's nothing to protect with a
        # login gate either -- only /setup and /oauth (the bootstrap flow
        # itself) are reachable until Drive is connected.
        if drive_sync_service.needs_setup():
            if path.startswith("/setup") or path.startswith("/oauth"):
                return await call_next(request)
            return RedirectResponse(url="/setup")

        login_password = get_instance_config().app_login_password
        if login_password and path != "/login" and not request.session.get("authenticated"):
            return RedirectResponse(url=f"/login?next={path}")

        return await call_next(request)

    app.add_middleware(SessionMiddleware, secret_key=get_instance_config().session_secret_key)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def index() -> RedirectResponse:
        if drive_sync_service.needs_setup():
            return RedirectResponse(url="/setup")
        return RedirectResponse(url="/items")

    app.include_router(auth_pages.router)
    app.include_router(shops_pages.router)
    app.include_router(shops_fragments.router)
    app.include_router(avatars_pages.router)
    app.include_router(items_pages.router)
    app.include_router(items_fragments.router)
    app.include_router(settings_pages.router)
    app.include_router(settings_fragments.router)
    app.include_router(oauth_router.router)
    app.include_router(downloads_router.router)

    return app


app = create_app()
