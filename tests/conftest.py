from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.migrate import run_migrations
from app.db.session import get_sessionmaker, reset_engine_for_tests
from app.models import Base


@pytest.fixture()
def db_session(tmp_path: Path) -> Iterator[Session]:
    # File-based (not :memory:) so WAL-mode/multi-connection behavior mirrors
    # production, per the project's testing strategy.
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_local()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def configured_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point app.config.get_settings() / the app.db.session engine at an isolated tmp_path.

    Does NOT create or migrate the local DB -- use this directly for
    bootstrap/first-run tests where the DB is expected to be absent, or via
    the `migrated_settings` fixture when a ready-to-use DB is needed.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    reset_engine_for_tests()
    yield get_settings()
    reset_engine_for_tests()
    get_settings.cache_clear()


@pytest.fixture()
def migrated_settings(configured_settings):
    configured_settings.data_dir.mkdir(parents=True, exist_ok=True)
    run_migrations()
    yield configured_settings


@pytest.fixture()
def app_db_session(migrated_settings) -> Iterator[Session]:
    session_local = get_sessionmaker()
    session = session_local()
    try:
        yield session
    finally:
        session.close()
