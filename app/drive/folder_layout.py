"""Drive folder layout conventions.

Root: `VRC-ContentManager/`, with exactly three top-level folders:

- `_db/app.db` -- the SQLite snapshot (see drive_sync_service).
- `upload/` -- a "reception" inbox. Files dropped here directly in Drive
  (outside the app) are picked up by drive_reconcile_service, imported as
  new items, and moved into `file/`.
- `file/` -- flat storage for every file the app manages (uploads made
  through the web UI land here directly; legacy per-avatar/shop nested
  files are migrated in by drive_reconcile_service).

get_or_create_folder always re-queries Drive by name+parent rather than
caching an id, so if the root (or any subfolder) is deleted directly in
Drive, the next ensure_*_folder call transparently recreates it -- no
special-cased "was it deleted?" handling needed.
"""

from __future__ import annotations

from app.drive.client import DriveClient

ROOT_FOLDER_NAME = "VRC-ContentManager"
DB_FOLDER_NAME = "_db"
DB_FILE_NAME = "app.db"
UPLOAD_FOLDER_NAME = "upload"
FILE_FOLDER_NAME = "file"


def ensure_folder_path(client: DriveClient, *segments: str) -> str:
    """Get-or-create each segment in order, returning the id of the final folder."""
    parent_id: str | None = None
    for segment in segments:
        parent_id = client.get_or_create_folder(segment, parent_id)
    assert parent_id is not None
    return parent_id


def ensure_db_folder(client: DriveClient) -> str:
    return ensure_folder_path(client, ROOT_FOLDER_NAME, DB_FOLDER_NAME)


def ensure_upload_folder(client: DriveClient) -> str:
    return ensure_folder_path(client, ROOT_FOLDER_NAME, UPLOAD_FOLDER_NAME)


def ensure_file_folder(client: DriveClient) -> str:
    return ensure_folder_path(client, ROOT_FOLDER_NAME, FILE_FOLDER_NAME)
