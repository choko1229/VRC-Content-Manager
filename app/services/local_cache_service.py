"""Local filesystem caching for item files.

Two independent caches, both under `settings.data_dir/cache`:

- `uploads/` (pending_upload_path): holds a freshly-received file between
  "accepted the upload" and "confirmed pushed to Google Drive" -- see
  upload_sync_service, which pushes these to Drive in the background and
  deletes the cached copy on success. Not time-limited; an entry only ever
  goes away via a successful sync or the owning item being deleted first.
- `downloads/` (download_cache_path): a 7-day, sliding-expiration cache of
  files fetched from Drive (downloads and thumbnail views), keyed by
  drive_file_id, so requesting the same file again soon after doesn't
  re-fetch it from Drive.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)

DOWNLOAD_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
_PURGE_INTERVAL_SECONDS = 6 * 60 * 60  # sweeping a few times a day is plenty for a 7-day TTL


def pending_upload_path(stored_filename: str) -> Path:
    cache_dir = get_settings().pending_upload_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / stored_filename


def download_cache_path(drive_file_id: str) -> Path:
    cache_dir = get_settings().download_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / drive_file_id


def peek_download_cached(drive_file_id: str) -> bool:
    """True if a fresh (within the TTL) cached copy exists. Read-only --
    does not touch the file's mtime, so status displays (item list/detail
    badges) can call this freely without artificially keeping an
    unrequested file's cache window alive forever. Use is_download_cached
    for an actual access."""
    path = download_cache_path(drive_file_id)
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < DOWNLOAD_CACHE_TTL_SECONDS


def is_download_cached(drive_file_id: str) -> bool:
    """True if a fresh (within the TTL) cached copy exists. Touches the
    file's mtime on a hit, so an actively-requested file's cache window
    keeps sliding forward rather than expiring on a fixed schedule. Only
    call this for a genuine access (serving a download/thumbnail) -- use
    peek_download_cached for a read-only status check."""
    if not peek_download_cached(drive_file_id):
        return False
    download_cache_path(drive_file_id).touch()
    return True


def forget_download(drive_file_id: str) -> None:
    """Removes a single download-cache entry immediately (e.g. the file it
    belongs to was deleted, so caching it further would be pointless)."""
    download_cache_path(drive_file_id).unlink(missing_ok=True)


def purge_expired_downloads() -> int:
    """Delete download-cache entries untouched for longer than the TTL. Returns the count removed."""
    cache_dir = get_settings().download_cache_dir
    if not cache_dir.exists():
        return 0
    removed = 0
    now = time.time()
    for entry in cache_dir.iterdir():
        if not entry.is_file():
            continue
        if now - entry.stat().st_mtime >= DOWNLOAD_CACHE_TTL_SECONDS:
            entry.unlink(missing_ok=True)
            removed += 1
    if removed:
        logger.info("purged %d expired download-cache entries", removed)
    return removed


async def purge_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_PURGE_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        try:
            await asyncio.to_thread(purge_expired_downloads)
        except Exception:
            logger.exception("local_cache_service: download-cache purge sweep failed")
