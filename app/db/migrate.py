"""Programmatic Alembic invocation so the app can migrate its own SQLite file on startup.

Used by the lifespan hook. In Phase 2 this becomes part of the Drive-backed
bootstrap sequence (download/create the DB, then migrate) instead of running
unconditionally against whatever local file happens to exist.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.config import get_settings

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def run_migrations() -> None:
    settings = get_settings()
    alembic_cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
    logger.info("running migrations against %s", settings.local_db_path)
    command.upgrade(alembic_cfg, "head")
