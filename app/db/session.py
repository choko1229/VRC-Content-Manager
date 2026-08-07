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

# A request that creates/edits/deletes an item runs its DB work on its own
# threadpool thread (FastAPI's run_in_threadpool), same as the background
# flows (upload_sync_service pushing a file, drive_sync_service's debounced
# snapshot/push, drive_reconcile_service's periodic sweep, item_service's
# dedup sweep). Uploading several files at once -- the whole point of the
# TOP page's multi-file drag-and-drop -- fires that many concurrent
# create_item_with_file calls, each on its own thread, each also scheduling
# its own fire-and-forget upload_sync_service push. SQLite only ever allows
# one writer at a time; WAL mode + PRAGMA busy_timeout (below) makes a late
# writer wait rather than fail immediately, but under real concurrent load
# from several of these at once the wait can still be exceeded (or lost to
# starvation against a stream of newer contenders), surfacing as "database is
# locked". db_write_lock serializes every DB-writing entrypoint at the Python
# level so they queue deterministically instead of racing SQLite's timeout.
#
# It's an RLock, not a plain Lock, specifically because some of these
# entrypoints call each other on the same thread (e.g. auto_merge_duplicate_
# products -> merge_item_into, merge_duplicate_group -> delete_item) -- a
# plain Lock would deadlock on that same-thread nested acquire; RLock only
# blocks a *different* thread, which is exactly what's needed here.
db_write_lock = threading.RLock()


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
