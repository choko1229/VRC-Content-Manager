"""Drive folder layout conventions.

Root: `BOOTH管理/`. The SQLite snapshot lives at `BOOTH管理/_db/app.db`.
Item assets live at `BOOTH管理/{対応アバター}/{ショップ名}_{商品名}/` (see
app/services/item_service.py, added in Phase 3, for how the avatar segment
is chosen when an item has zero or multiple avatars).
"""

from __future__ import annotations

from app.drive.client import DriveClient

ROOT_FOLDER_NAME = "BOOTH管理"
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
