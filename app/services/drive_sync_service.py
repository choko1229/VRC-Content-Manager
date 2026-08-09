"""Google Drive sync strategy for the local SQLite file.

- Bootstrap (see `complete_first_run_setup`): first run has no local DB. The
  app defers DB creation until OAuth completes (token can't be persisted
  before a DB exists), then either downloads an existing snapshot (if a
  Drive DB file id was given on /setup -- disaster recovery) or creates
  +migrates a fresh DB and uploads it (true first run).
- Write-back: services call `mark_dirty()` after a successful local commit.
  A background loop (`sync_loop`) flushes on a timer (DB-backed
  `sync_interval_seconds`, see app_config_service) rather than per-write, to
  avoid thrashing the Drive API quota. Also flushed once on graceful
  shutdown and via a manual "sync now" action.
- Lock scope: `db_write_lock` (see app/db/session.py) is only ever held
  around the local SQLite reads/writes in this module -- never around a
  Drive network call. Every interactive item edit also takes this same
  lock, so a network round-trip held under it (this used to wrap the whole
  snapshot+upload) would stall every save in the app for however long that
  request takes, which is unrelated to what the lock actually protects
  against (SQLite's busy_timeout race between concurrent local writers).
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
from app.core.exceptions import DriveError
from app.db.migrate import run_migrations
from app.db.session import db_write_lock, get_sessionmaker
from app.drive import folder_layout
from app.drive.client import DriveClient
from app.drive.google_drive_client import GoogleDriveClient
from app.services import app_config_service, app_settings_service, oauth_service

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
        # This is a raw connection, not one made through app.db.session's
        # engine -- it doesn't inherit that engine's "connect" event
        # listener, so the busy timeout needs setting again here too.
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(f"VACUUM INTO '{escaped}'")


def complete_first_run_setup(
    credentials: Credentials,
    *,
    drive_db_file_id: str | None = None,
    drive_client: DriveClient | None = None,
) -> None:
    """Called once, right after the OAuth callback succeeds on a machine with no local DB yet.

    `drive_db_file_id` comes from the /setup form (disaster recovery: restore an existing
    Drive-hosted database instead of creating a fresh one). `drive_client` is injectable so
    tests can pass a FakeDriveClient instead of hitting real Google Drive; production callers
    (app/api/routers/oauth.py) always omit it.
    """
    settings = get_settings()
    settings.local_db_path.parent.mkdir(parents=True, exist_ok=True)
    drive_client = drive_client if drive_client is not None else GoogleDriveClient(credentials)

    if drive_db_file_id:
        logger.info("restoring existing database from Drive (drive_db_file_id=%s)", drive_db_file_id)
        drive_client.download_file(file_id=drive_db_file_id, dest_path=settings.local_db_path)
        run_migrations()  # idempotent; catches up if the snapshot predates a newer schema
    else:
        logger.info("no existing Drive database specified; creating a fresh database and uploading it")
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
    modified_time_iso: str | None = None
    try:
        meta = drive_client.get_metadata(drive_file_id)
        if meta.modified_time:
            modified_time_iso = meta.modified_time.isoformat()
    except Exception:
        logger.exception("could not confirm Drive modifiedTime after push (non-fatal)")

    with db_write_lock:
        app_settings_service.set_setting(db, _SETTING_LAST_PUSHED_AT, now.isoformat())
        if modified_time_iso:
            app_settings_service.set_setting(db, _SETTING_LAST_KNOWN_REMOTE_MODIFIED, modified_time_iso)


def flush_now(db: Session, *, drive_client: DriveClient | None = None) -> bool:
    """Snapshot + push the local DB to Drive if dirty. Returns True if a push happened.

    `drive_client` is injectable for tests (FakeDriveClient); production callers omit it
    and get a real, credential-backed client via oauth_service.
    """
    global _dirty
    if not _dirty:
        return False

    with db_write_lock:
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

    # Everything from here on is either a Drive network call or (briefly,
    # internally locked) a local snapshot copy -- deliberately outside
    # db_write_lock so this doesn't stall interactive saves for however long
    # the upload takes. See the module docstring.
    drive_file_id = _ensure_db_file_exists(db, drive_client, drive_file_id)

    settings = get_settings()
    tmp_snapshot = settings.data_dir / "tmp" / f"sync_snapshot_{uuid.uuid4().hex}.db"
    try:
        with db_write_lock:
            snapshot_db_to(tmp_snapshot)
            # Reset now, while still holding the lock, rather than after the
            # (unlocked) upload below -- any write that lands once this lock
            # is released is naturally captured for the *next* flush cycle
            # instead of being silently dropped by a late reset here.
            _dirty = False
        drive_client.update_file_content(
            file_id=drive_file_id, local_path=tmp_snapshot, mime_type="application/x-sqlite3"
        )
    finally:
        tmp_snapshot.unlink(missing_ok=True)

    _record_push_metadata(db, drive_client, drive_file_id)
    logger.info("synced local database to Drive (file_id=%s)", drive_file_id)
    return True


def _ensure_db_file_exists(db: Session, drive_client: DriveClient, drive_file_id: str) -> str:
    """Recover if the Drive file drive_db_file_id points at is gone (e.g. the
    user deleted the whole root folder by hand).

    Item-asset folders already self-heal on the next upload because
    ensure_file_folder re-queries Drive by name/parent instead of caching an
    id (see folder_layout.py) -- but the DB snapshot is referenced by a fixed
    file id with no equivalent "look it up again" step, so it needed this
    explicit check. Recreates the _db folder (itself self-healing even if the
    whole root was deleted) and uploads a fresh snapshot as a new file,
    persisting the new id so future syncs use it.
    """
    try:
        drive_client.get_metadata(drive_file_id)
        return drive_file_id
    except DriveError:
        logger.warning(
            "Drive DB file id=%s no longer resolves (deleted?); recreating the _db folder "
            "and uploading a fresh snapshot",
            drive_file_id,
        )

    db_folder_id = folder_layout.ensure_db_folder(drive_client)
    settings = get_settings()
    tmp_snapshot = settings.data_dir / "tmp" / f"recover_snapshot_{uuid.uuid4().hex}.db"
    try:
        with db_write_lock:
            snapshot_db_to(tmp_snapshot)
        drive_file = drive_client.upload_file(
            local_path=tmp_snapshot,
            name=folder_layout.DB_FILE_NAME,
            parent_id=db_folder_id,
            mime_type="application/x-sqlite3",
        )
    finally:
        tmp_snapshot.unlink(missing_ok=True)

    with db_write_lock:
        app_settings_service.set_setting(db, _SETTING_DRIVE_DB_FILE_ID, drive_file.id)
    logger.info("recreated Drive DB file (new drive_db_file_id=%s)", drive_file.id)
    return drive_file.id


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


def _get_sync_interval_seconds() -> int:
    if needs_setup():
        # No DB yet (first run, still on /setup) -- nothing to sync, and the
        # app_settings table doesn't exist to query yet either.
        return app_config_service.DEFAULT_SYNC_INTERVAL_SECONDS
    session_local = get_sessionmaker()
    with session_local() as db:
        return app_config_service.get_sync_interval_seconds(db)


async def sync_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        # Re-read each cycle (not just once) so a change made on /settings
        # takes effect on the next tick without requiring a restart.
        interval = await asyncio.to_thread(_get_sync_interval_seconds)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        if _dirty:
            await flush_now_async()
