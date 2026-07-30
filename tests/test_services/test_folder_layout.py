from __future__ import annotations

from app.drive import folder_layout
from app.drive.fake_drive_client import FakeDriveClient


def test_ensure_folder_path_creates_nested_folders_once() -> None:
    client = FakeDriveClient()

    first = folder_layout.ensure_folder_path(client, "BOOTH管理", "アバターA", "ショップ_商品")
    second = folder_layout.ensure_folder_path(client, "BOOTH管理", "アバターA", "ショップ_商品")

    assert first == second  # idempotent: re-resolving the same path doesn't create duplicates


def test_ensure_item_folder_falls_back_to_unassigned_bucket() -> None:
    client = FakeDriveClient()

    folder_id = folder_layout.ensure_item_folder(
        client, avatar_name=None, shop_name="MyShop", item_name="CoolItem"
    )

    root_id = client.get_or_create_folder(folder_layout.ROOT_FOLDER_NAME)
    unassigned_id = client.get_or_create_folder(folder_layout.UNASSIGNED_AVATAR_FOLDER_NAME, root_id)
    item_id = client.get_or_create_folder("MyShop_CoolItem", unassigned_id)
    assert folder_id == item_id
