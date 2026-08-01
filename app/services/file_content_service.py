"""Resolves an ItemFile to a local filesystem path ready to serve, doing
whatever caching/downloading is needed along the way.

Never deletes the returned path -- it's either the pending-upload cache
(the file hasn't reached Drive yet) or the download cache (a copy fetched
from Drive, kept for local_cache_service.DOWNLOAD_CACHE_TTL_SECONDS), and
both have their own lifecycle managed elsewhere.
"""

from __future__ import annotations

from pathlib import Path

from app.core.exceptions import DriveError
from app.drive.client import DriveClient
from app.models.item_file import ItemFile
from app.services import local_cache_service


def resolve_local_path(item_file: ItemFile, drive_client: DriveClient | None) -> Path:
    if item_file.synced_at is None:
        # Only local copy is the pending-upload cache -- no Drive call needed.
        path = local_cache_service.pending_upload_path(item_file.stored_filename)
        if not path.exists():
            raise DriveError(f"pending file missing from local cache (item_file id={item_file.id})")
        return path

    if not item_file.drive_file_id:
        raise DriveError(f"item_file id={item_file.id} is marked synced but has no drive_file_id")

    if local_cache_service.is_download_cached(item_file.drive_file_id):
        return local_cache_service.download_cache_path(item_file.drive_file_id)

    if drive_client is None:
        raise DriveError(f"item_file id={item_file.id} needs a Drive fetch but no client was given")

    cache_path = local_cache_service.download_cache_path(item_file.drive_file_id)
    drive_client.download_file(file_id=item_file.drive_file_id, dest_path=cache_path)
    return cache_path
