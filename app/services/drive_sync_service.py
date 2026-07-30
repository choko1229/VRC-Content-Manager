"""Google Drive sync strategy for the local SQLite file.

- Bootstrap (see `complete_first_run_setup`): first run has no local DB. The
  app defers DB creation until OAuth completes (token can't be persisted
  before a DB exists), then either downloads an existing snapshot
  (DRIVE_DB_FILE_ID set -- disaster recovery) or creates+migrates a fresh
  DB and uploads it (true first run).
- Write-back: services call `mark_dirty()` after a successful local commit.
  A background loop (`sync_loop`) flushes on a timer (SYNC_INTERVAL_SECONDS)
  rather than per-write, to avoid thrashing the Drive API quota. Also
  flushed once on graceful shutdown and via a manual "sync now" action.
- Snapshotting: the live SQLite file is never uploaded directly. `VACUUM
  INTO` takes a consistent point-in-time copy first, so a page mid-write is
  never uploaded. Requires `--workers 1` (single writer) by design.
- Conflict detection: on a normal restart with an existing local DB, the
  Drive file's modifiedTime is compared against the last known push time.
  If Drive looks newer, this logs a loud warning instead of silently
  overwriting -- there is no auto-merge (single-writer assumption).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.migrate import run_migrations
from app.db.session import get_sessionmaker
from app.drive import folder_layout
from app.drive.client import DriveClient
from app.drive.google_drive_client import GoogleDriveClient
from app.services import app_settings_service, oauth_service

logger = logging.getLogger(__name__)

_SETTING_DRIVE_DB_FILE_ID = "drive_db_file_id"
_SETTING_LAST_PUSHED_AT = "drive_db_last_pushed_at"
_SETTING_LAST_KNOWN_REMOTE_MODIFIED = "drive_db_last_known_modified_time"

_dirty = False


def needs_setup() -> bool:
    return not get_settings().local_db_path.exists()


def mark_dirty() -> None:
    global _dirty
    _dirty = True


def is_dirty() -> bool:
    return _dirty


def snapshot_db_to(dest_path: Path) -> None:
    settings = get_settings()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if dest_path.exists():
        dest_path.unlink()
    # VACUUM INTO doesn't reliably support `?` parameter binding across
    # sqlite versions; dest_path is always internally constructed (never
    # user input), so a quote-escaped literal is safe here.
    escaped = str(dest_path).replace("'", "''")
    with sqlite3.connect(str(settings.local_db_path)) as conn:
        conn.execute(f"VACUUM INTO '{escaped}'")


def complete_first_run_setup(credentials: Credentials, *, drive_client: DriveClient | None = None) -> None:
    """Called once, right after the OAuth callback succeeds on a machine with no local DB yet.

    `drive_client` is injectable so tests can pass a FakeDriveClient instead of hitting real
    Google Drive; production callers (app/api/routers/oauth.py) always omit it.
    """
    settings = get_settings()
    settings.local_db_path.parent.mkdir(parents=True, exist_ok=True)
    drive_client = drive_client if drive_client is not None else GoogleDriveClient(credentials)

    if settings.drive_db_file_id:
        logger.info("DRIVE_DB_FILE_ID set; restoring existing database from Drive")
        drive_client.download_file(file_id=settings.drive_db_file_id, dest_path=settings.local_db_path)
        run_migrations()  # idempotent; catches up if the snapshot predates a newer schema
        drive_db_file_id = settings.drive_db_file_id
    else:
        logger.info("no DRIVE_DB_FILE_ID; creating a fresh database and uploading it to Drive")
        run_migrations()
        db_folder_id = folder_layout.ensure_db_folder(drive_client)
        tmp_snapshot = settings.data_dir / "tmp" / f"initial_snapshot_{uuid.uuid4().hex}.db"
        try:
            snapshot_db_to(tmp_snapshot)
            drive_file = drive_client.upload_file(
                local_path=tmp_snapshot,
                name=folder_layout.DB_FILE_NAME,
                parent_id=db_folder_id,
                mime_type="application/x-sqlite3",
            )
        finally:
            tmp_snapshot.unlink(missing_ok=True)
        drive_db_file_id = drive_file.id

    session_local = get_sessionmaker()
    with session_local() as db:
        oauth_service.save_credentials(db, credentials)
        app_settings_service.set_setting(db, _SETTING_DRIVE_DB_FILE_ID, drive_db_file_id)
        _record_push_metadata(db, drive_client, drive_db_file_id)
    logger.info("first-run setup complete (drive_db_file_id=%s)", drive_db_file_id)


def _record_push_metadata(db: Session, drive_client: DriveClient, drive_file_id: str) -> None:
    now = datetime.now(timezone.utc)
    app_settings_service.set_setting(db, _SETTING_LAST_PUSHED_AT, now.isoformat())
    try:
        meta = drive_client.get_metadata(drive_file_id)
        if meta.modified_time:
            app_settings_service.set_setting(
                db, _SETTING_LAST_KNOWN_REMOTE_MODIFIED, meta.modified_time.isoformat()
            )
    except Exception:
        logger.exception("could not confirm Drive modifiedTime after push (non-fatal)")


def flush_now(db: Session, *, drive_client: DriveClient | None = None) -> bool:
    """Snapshot + push the local DB to Drive if dirty. Returns True if a push happened.

    `drive_client` is injectable for tests (FakeDriveClient); production callers omit it
    and get a real, credential-backed client via oauth_service.
    """
    global _dirty
    if not _dirty:
        return False

    drive_file_id = app_settings_service.get_setting(db, _SETTING_DRIVE_DB_FILE_ID)
    if not drive_file_id:
        logger.warning("skipping Drive sync: no drive_db_file_id recorded yet (was /setup completed?)")
        return False

    if drive_client is None:
        try:
            drive_client = oauth_service.make_drive_client(db)
        except oauth_service.NotConnectedError:
            logger.warning("skipping Drive sync: not connected")
            return False

    settings = get_settings()
    tmp_snapshot = settings.data_dir / "tmp" / f"sync_snapshot_{uuid.uuid4().hex}.db"
    try:
        snapshot_db_to(tmp_snapshot)
        drive_client.update_file_content(
            file_id=drive_file_id, local_path=tmp_snapshot, mime_type="application/x-sqlite3"
        )
    finally:
        tmp_snapshot.unlink(missing_ok=True)

    _record_push_metadata(db, drive_client, drive_file_id)
    _dirty = False
    logger.info("synced local database to Drive (file_id=%s)", drive_file_id)
    return True


def check_remote_drift(db: Session, *, drive_client: DriveClient | None = None) -> None:
    """Best-effort warning if Drive's copy looks newer than our last known push. No auto-merge."""
    if not oauth_service.is_connected(db):
        return
    drive_file_id = app_settings_service.get_setting(db, _SETTING_DRIVE_DB_FILE_ID)
    if not drive_file_id:
        return

    try:
        if drive_client is None:
            drive_client = oauth_service.make_drive_client(db)
        meta = drive_client.get_metadata(drive_file_id)
    except Exception:
        logger.exception("could not check Drive for remote drift; continuing with local copy")
        return

    last_known_raw = app_settings_service.get_setting(db, _SETTING_LAST_KNOWN_REMOTE_MODIFIED)
    if meta.modified_time and last_known_raw:
        last_known = datetime.fromisoformat(last_known_raw)
        if meta.modified_time > last_known:
            logger.warning(
                "Drive's app.db (modifiedTime=%s) looks newer than our last known push (%s). "
                "This process assumes a single writer and will NOT auto-merge -- investigate "
                "before making further changes if this is unexpected.",
                meta.modified_time,
                last_known,
            )


def _flush_blocking() -> bool:
    session_local = get_sessionmaker()
    with session_local() as db:
        try:
            return flush_now(db)
        except Exception:
            logger.exception("Drive sync flush failed")
            return False


async def flush_now_async() -> bool:
    return await asyncio.to_thread(_flush_blocking)


async def sync_loop(stop_event: asyncio.Event) -> None:
    interval = get_settings().sync_interval_seconds
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        if _dirty:
            await flush_now_async()
