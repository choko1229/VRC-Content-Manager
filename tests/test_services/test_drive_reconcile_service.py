from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.drive import folder_layout
from app.drive.fake_drive_client import FakeDriveClient
from app.models.item import Item
from app.schemas.item import ItemCreate
from app.services import drive_reconcile_service, item_service
from app.services.upload_service import ValidatedUpload


def _make_upload(tmp_path: Path, name: str, content: bytes = b"dummy") -> ValidatedUpload:
    path = tmp_path / name
    path.write_bytes(content)
    return ValidatedUpload(
        path=path, original_filename=name, size_bytes=len(content), content_type="application/octet-stream", extension=".zip"
    )


def test_reconcile_removes_item_file_deleted_directly_on_drive(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    created = item_service.create_item_with_file(
        app_db_session,
        data=ItemCreate(name="Will Lose File", shop_name="Shop"),
        primary_upload=_make_upload(tmp_path, "asset.zip"),
        drive_client=fake_client,
    )
    item = app_db_session.get(Item, created.id)
    drive_file_id = item.files[0].drive_file_id

    # Simulate a manual delete directly in Drive, bypassing the app.
    del fake_client._files[drive_file_id]

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.removed_broken_files == 1
    assert result.imported_items == 0
    refreshed = app_db_session.get(Item, created.id)
    assert refreshed is not None  # the item itself survives
    assert refreshed.files == []  # just the stale file reference is gone


def test_reconcile_leaves_intact_references_alone(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    created = item_service.create_item_with_file(
        app_db_session,
        data=ItemCreate(name="Still Fine", shop_name="Shop"),
        primary_upload=_make_upload(tmp_path, "asset.zip"),
        drive_client=fake_client,
    )

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.removed_broken_files == 0
    assert result.imported_items == 0
    item = app_db_session.get(Item, created.id)
    assert len(item.files) == 1


def test_reconcile_imports_file_added_directly_on_drive(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    root_id = folder_layout.ensure_folder_path(fake_client, folder_layout.ROOT_FOLDER_NAME)
    avatar_id = fake_client.get_or_create_folder("Manuka", root_id)
    item_folder_id = fake_client.get_or_create_folder("SomeShop_CoolOutfit", avatar_id)
    asset_path = tmp_path / "outfit.zip"
    asset_path.write_bytes(b"zip bytes")
    fake_client.upload_file(local_path=asset_path, name="outfit.zip", parent_id=item_folder_id, mime_type="application/zip")

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.removed_broken_files == 0
    assert result.imported_items == 1
    items = app_db_session.execute(select(Item)).scalars().all()
    assert len(items) == 1
    assert items[0].name == "outfit"
    assert items[0].shop.name == "未設定"
    assert [a.name for a in items[0].avatars] == ["Manuka"]
    assert items[0].files[0].file_role.value == "primary"


def test_reconcile_pairs_sibling_image_as_thumbnail(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    root_id = folder_layout.ensure_folder_path(fake_client, folder_layout.ROOT_FOLDER_NAME)
    unassigned_id = fake_client.get_or_create_folder(folder_layout.UNASSIGNED_AVATAR_FOLDER_NAME, root_id)
    item_folder_id = fake_client.get_or_create_folder("SomeShop_ThumbTest", unassigned_id)
    asset_path = tmp_path / "thing.unitypackage"
    asset_path.write_bytes(b"pkg bytes")
    thumb_path = tmp_path / "thing.png"
    thumb_path.write_bytes(b"\x89PNG\r\n")
    fake_client.upload_file(local_path=asset_path, name="thing.unitypackage", parent_id=item_folder_id)
    fake_client.upload_file(local_path=thumb_path, name="thing.png", parent_id=item_folder_id, mime_type="image/png")

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.imported_items == 1
    item = app_db_session.execute(select(Item)).scalars().one()
    assert item.avatars == []  # UNASSIGNED_AVATAR_FOLDER_NAME never becomes an avatar tag
    roles = {f.file_role.value for f in item.files}
    assert roles == {"primary", "thumbnail"}


def test_reconcile_skips_orphan_image_with_no_asset_file(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    root_id = folder_layout.ensure_folder_path(fake_client, folder_layout.ROOT_FOLDER_NAME)
    avatar_id = fake_client.get_or_create_folder("Manuka", root_id)
    item_folder_id = fake_client.get_or_create_folder("SomeShop_JustAnImage", avatar_id)
    thumb_path = tmp_path / "lonely.png"
    thumb_path.write_bytes(b"\x89PNG\r\n")
    fake_client.upload_file(local_path=thumb_path, name="lonely.png", parent_id=item_folder_id, mime_type="image/png")

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.imported_items == 0
    assert app_db_session.execute(select(Item)).scalars().all() == []


def test_reconcile_ignores_files_outside_allowed_extensions(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    root_id = folder_layout.ensure_folder_path(fake_client, folder_layout.ROOT_FOLDER_NAME)
    avatar_id = fake_client.get_or_create_folder("Manuka", root_id)
    item_folder_id = fake_client.get_or_create_folder("SomeShop_RandomFile", avatar_id)
    stray_path = tmp_path / "notes.txt"
    stray_path.write_bytes(b"just some notes")
    fake_client.upload_file(local_path=stray_path, name="notes.txt", parent_id=item_folder_id)

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.imported_items == 0
    assert app_db_session.execute(select(Item)).scalars().all() == []


def test_reconcile_regenerates_root_folder_if_deleted(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    root_id = folder_layout.ensure_folder_path(fake_client, folder_layout.ROOT_FOLDER_NAME)
    del fake_client._files[root_id]  # simulate deleting the whole root folder directly in Drive

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.removed_broken_files == 0
    assert result.imported_items == 0
    new_root_id = fake_client.get_or_create_folder(folder_layout.ROOT_FOLDER_NAME)
    assert new_root_id != root_id  # a fresh folder was created, not the deleted one
