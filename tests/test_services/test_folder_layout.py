from __future__ import annotations

from app.drive import folder_layout
from app.drive.fake_drive_client import FakeDriveClient


def test_ensure_folder_path_creates_nested_folders_once() -> None:
    client = FakeDriveClient()

    first = folder_layout.ensure_folder_path(client, "BOOTH管理", "アバターA", "ショップ_商品")
    second = folder_layout.ensure_folder_path(client, "BOOTH管理", "アバターA", "ショップ_商品")

    assert first == second  # idempotent: re-resolving the same path doesn't create duplicates


def test_ensure_file_folder_is_idempotent() -> None:
    client = FakeDriveClient()

    first = folder_layout.ensure_file_folder(client)
    second = folder_layout.ensure_file_folder(client)

    root_id = client.get_or_create_folder(folder_layout.ROOT_FOLDER_NAME)
    file_id = client.get_or_create_folder(folder_layout.FILE_FOLDER_NAME, root_id)
    assert first == second == file_id


def test_ensure_upload_folder_is_idempotent() -> None:
    client = FakeDriveClient()

    first = folder_layout.ensure_upload_folder(client)
    second = folder_layout.ensure_upload_folder(client)

    root_id = client.get_or_create_folder(folder_layout.ROOT_FOLDER_NAME)
    upload_id = client.get_or_create_folder(folder_layout.UPLOAD_FOLDER_NAME, root_id)
    assert first == second == upload_id
