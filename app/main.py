from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db.migrate import run_migrations
from app.logging_conf import configure_logging
from app.web.fragments import shops as shops_fragments
from app.web.pages import shops as shops_pages

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.upload_tmp_dir.mkdir(parents=True, exist_ok=True)
    # Phase 1: migrate whatever local DB is present. Phase 2 replaces this
    # with the full Drive-backed bootstrap (download-or-create, then migrate).
    run_migrations()
    logger.info("startup complete (data_dir=%s)", settings.data_dir)
    yield
    logger.info("shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="BOOTH Asset Manager", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def index() -> RedirectResponse:
        return RedirectResponse(url="/shops")

    app.include_router(shops_pages.router)
    app.include_router(shops_fragments.router)

    return app


app = create_app()
