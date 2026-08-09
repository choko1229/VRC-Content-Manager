"""Background push of newly-uploaded files to Google Drive.

Uploads are accepted fast: item_service.create_item_with_file caches the
file locally (local_cache_service.pending_upload_path) and commits the DB
row immediately with synced_at=None, drive_file_id=None -- there is no
Drive call in the request path at all. This service does the actual Drive
upload afterward:

- sync_pending_now() scans every ItemFile with synced_at IS NULL and pushes
  each to Drive. The upload route fires this once, immediately, right after
  a request that created a pending file (fire-and-forget, not awaited, so
  the response doesn't wait on Drive) for near-instant sync.
- sync_loop() is a periodic fallback sweep calling the same function, so
  anything missed (a process restart mid-sync, a transient Drive failure)
  still gets picked up without user action.

Both funnel through sync_item_file, which item_service also calls directly
when a caller (tests, or any explicit-sync use case) hands
create_item_with_file/update_item a `drive_client` -- see there. A
thread-safe in-memory "claim" set guards
against the immediate fire and a sweep tick both picking up the same row
at once, which would otherwise upload the same local file to Drive twice.

`db_write_lock` (see app/db/session.py) is only ever held around the local
DB reads/writes here, never around the Drive upload itself -- an item edit
takes the same lock, and a network call held under it would stall every
save in the app for however long that upload takes, for no protective
reason (busy_timeout races are a purely local-SQLite concern).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import DriveError
from app.db.session import db_write_lock, get_sessionmaker
from app.drive import folder_layout
from app.drive.client import DriveClient
from app.models.item_file import ItemFile
from app.services import drive_sync_service, local_cache_service, oauth_service

logger = logging.getLogger(__name__)

_SYNC_INTERVAL_SECONDS = 30

_claim_lock = threading.Lock()
_claimed: set[int] = set()


def _try_claim(item_file_id: int) -> bool:
    with _claim_lock:
        if item_file_id in _claimed:
            return False
        _claimed.add(item_file_id)
        return True


def _release(item_file_id: int) -> None:
    with _claim_lock:
        _claimed.discard(item_file_id)


def sync_item_file(db: Session, item_file_id: int, drive_client: DriveClient) -> bool:
    with db_write_lock:
        item_file = db.get(ItemFile, item_file_id)
        if item_file is None or item_file.synced_at is not None:
            return False  # deleted, or already synced by a racing call
        stored_filename = item_file.stored_filename
        original_filename = item_file.original_filename
        content_type = item_file.content_type

    local_path = local_cache_service.pending_upload_path(stored_filename)
    if not local_path.exists():
        logger.error(
            "MANUAL ATTENTION NEEDED: pending upload cache file missing for item_file id=%s (%s) -- "
            "cannot sync to Drive, dropping the reference so the item at least stops looking broken",
            item_file_id,
            stored_filename,
        )
        with db_write_lock:
            # populate_existing: force a fresh read instead of returning a
            # possibly-stale copy from the session's identity map -- see the
            # note on the re-fetch below.
            item_file = db.get(ItemFile, item_file_id, populate_existing=True)
            if item_file is not None:
                db.delete(item_file)
                db.commit()
        return False

    try:
        folder_id = folder_layout.ensure_file_folder(drive_client)
        drive_file = drive_client.upload_file(
            local_path=local_path,
            name=original_filename,
            parent_id=folder_id,
            mime_type=content_type,
        )
    except DriveError:
        logger.warning(
            "upload_sync_service: failed to push item_file id=%s to Drive (will retry)",
            item_file_id,
            exc_info=True,
        )
        return False

    with db_write_lock:
        # Re-fetch with populate_existing=True: the file may have been
        # deleted by another session while the upload above was in flight
        # (unlocked) -- a plain db.get() would silently return the stale
        # cached object from this session's identity map instead of
        # reflecting that, since nothing committed on this session since it
        # was first loaded.
        item_file = db.get(ItemFile, item_file_id, populate_existing=True)
        if item_file is None or item_file.synced_at is not None:
            return False
        item_file.drive_file_id = drive_file.id
        item_file.drive_folder_id = folder_id
        item_file.synced_at = datetime.now(timezone.utc)
        db.commit()
    local_path.unlink(missing_ok=True)
    drive_sync_service.mark_dirty()
    logger.info("upload_sync_service: synced item_file id=%s to Drive (drive_file_id=%s)", item_file_id, drive_file.id)
    return True


def sync_pending_now() -> int:
    """Synchronous entry point -- call via run_in_threadpool/asyncio.to_thread.

    Returns the number of files successfully synced this pass.
    """
    session_local = get_sessionmaker()
    with session_local() as db:
        try:
            drive_client = oauth_service.make_drive_client(db)
        except oauth_service.NotConnectedError:
            logger.info("upload_sync_service: Drive not connected, skipping pending sync")
            return 0

        with db_write_lock:
            pending_ids = list(db.execute(select(ItemFile.id).where(ItemFile.synced_at.is_(None))).scalars().all())

        # sync_item_file only takes db_write_lock around its own local reads/
        # writes (see there) -- not held here across the whole sweep, so an
        # in-progress upload doesn't stall unrelated interactive saves.
        synced = 0
        for item_file_id in pending_ids:
            if not _try_claim(item_file_id):
                continue
            try:
                if sync_item_file(db, item_file_id, drive_client):
                    synced += 1
            finally:
                _release(item_file_id)
        return synced


async def sync_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_SYNC_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        try:
            await asyncio.to_thread(sync_pending_now)
        except Exception:
            logger.exception("upload_sync_service: periodic sync sweep failed")
