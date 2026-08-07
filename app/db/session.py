"""Engine/session setup.

WAL journal mode + a busy timeout are enabled so that the single-writer,
single-worker-process design (see app/services/drive_sync_service.py) tolerates
the app's own concurrent reads/writes without "database is locked" errors.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None

# Several independent background flows (upload_sync_service pushing a
# just-uploaded file, drive_sync_service's debounced DB snapshot/push,
# drive_reconcile_service's periodic sweep, item_service's dedup sweep) each
# run on their own thread (via asyncio.to_thread/run_in_threadpool) and can
# fire close together -- e.g. several quick-uploads in a row each schedule
# their own fire-and-forget sync. SQLite only ever allows one writer at a
# time; WAL mode + PRAGMA busy_timeout (below) makes a late writer wait
# rather than fail immediately, but under real concurrent load from several
# of these flows at once the wait can still be exceeded, surfacing as
# "database is locked". background_write_lock serializes each flow's DB work
# at the Python level so they queue deterministically instead of racing
# SQLite's timeout. Only the four background entrypoints (reconcile(),
# flush_now(), sync_pending_now(), auto_merge_duplicate_products()) hold it,
# and none of them call into each other, so there's no risk of a nested
# (and therefore deadlocking, since threading.Lock isn't reentrant) acquire.
background_write_lock = threading.Lock()


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def get_db() -> Iterator[Session]:
    session_local = get_sessionmaker()
    db = session_local()
    try:
        yield db
    finally:
        db.close()


def reset_engine_for_tests() -> None:
    """Drop the cached engine/sessionmaker so tests can rebind to a fresh DB URL."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
