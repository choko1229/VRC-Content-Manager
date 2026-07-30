from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.config import get_settings
from app.logging_conf import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.upload_tmp_dir.mkdir(parents=True, exist_ok=True)
    logger.info("startup complete (data_dir=%s)", settings.data_dir)
    yield
    logger.info("shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="BOOTH Asset Manager", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
