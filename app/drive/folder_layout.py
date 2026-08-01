"""Drive folder layout conventions.

Root: `VRC-ContentManager/`. The SQLite snapshot lives at
`VRC-ContentManager/_db/app.db`. Item assets live at
`VRC-ContentManager/{対応アバター}/{ショップ名}_{商品名}/` (see
app/services/item_service.py, added in Phase 3, for how the avatar segment
is chosen when an item has zero or multiple avatars).

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
UNASSIGNED_AVATAR_FOLDER_NAME = "汎用"


def ensure_folder_path(client: DriveClient, *segments: str) -> str:
    """Get-or-create each segment in order, returning the id of the final folder."""
    parent_id: str | None = None
    for segment in segments:
        parent_id = client.get_or_create_folder(segment, parent_id)
    assert parent_id is not None
    return parent_id


def ensure_db_folder(client: DriveClient) -> str:
    return ensure_folder_path(client, ROOT_FOLDER_NAME, DB_FOLDER_NAME)


def ensure_item_folder(client: DriveClient, *, avatar_name: str | None, shop_name: str, item_name: str) -> str:
    avatar_segment = avatar_name or UNASSIGNED_AVATAR_FOLDER_NAME
    item_segment = f"{shop_name}_{item_name}"
    return ensure_folder_path(client, ROOT_FOLDER_NAME, avatar_segment, item_segment)
