from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.drive.fake_drive_client import _FOLDER_MIME_TYPE, FakeDriveClient
from app.models.item import Item
from app.models.item_file import FileRole
from app.schemas.item import ItemCreate
from app.services import drive_sync_service, item_service, shop_service
from app.services.upload_service import ValidatedUpload


def _make_upload(
    tmp_path: Path, name: str = "avatar.unitypackage", content: bytes = b"dummy content", extension: str = ".unitypackage"
) -> ValidatedUpload:
    path = tmp_path / name
    path.write_bytes(content)
    return ValidatedUpload(
        path=path,
        original_filename=name,
        size_bytes=len(content),
        content_type="application/octet-stream",
        extension=extension,
    )


@pytest.fixture(autouse=True)
def _reset_dirty(app_db_session: Session):
    drive_sync_service._dirty = False
    yield
    drive_sync_service._dirty = False


def test_create_item_with_file_success(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    upload = _make_upload(tmp_path)
    data = ItemCreate(name="Cool Avatar", shop_name="Test Shop", tags=["衣装", "改変可"], avatars=["Manuka"])

    result = item_service.create_item_with_file(
        app_db_session, data=data, primary_upload=upload, drive_client=fake_client
    )

    assert result.name == "Cool Avatar"
    assert result.shop_name == "Test Shop"
    assert sorted(result.tags) == ["改変可", "衣装"]
    assert result.avatars == ["Manuka"]
    assert drive_sync_service.is_dirty() is True

    item = app_db_session.get(Item, result.id)
    assert item is not None
    assert len(item.files) == 1
    assert item.files[0].file_role == FileRole.PRIMARY
    assert item.license is not None


def test_create_item_with_file_reuses_existing_shop(app_db_session: Session, tmp_path: Path) -> None:
    shop = shop_service.get_or_create_shop(app_db_session, name="Existing Shop", url=None)
    app_db_session.commit()

    fake_client = FakeDriveClient()
    upload = _make_upload(tmp_path)
    data = ItemCreate(name="Item A", shop_name="Existing Shop")

    item_service.create_item_with_file(app_db_session, data=data, primary_upload=upload, drive_client=fake_client)

    item = app_db_session.execute(select(Item).where(Item.name == "Item A")).scalar_one()
    assert item.shop_id == shop.id


def test_create_item_with_file_uploads_thumbnail_too(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    upload = _make_upload(tmp_path)
    thumbnail = _make_upload(tmp_path, name="thumb.png", content=b"\x89PNG\r\n", extension=".png")
    data = ItemCreate(name="With Thumb", shop_name="Shop")

    result = item_service.create_item_with_file(
        app_db_session, data=data, primary_upload=upload, thumbnail_upload=thumbnail, drive_client=fake_client
    )

    assert result.has_thumbnail is True
    item = app_db_session.get(Item, result.id)
    roles = {f.file_role for f in item.files}
    assert roles == {FileRole.PRIMARY, FileRole.THUMBNAIL}


def test_create_item_with_file_compensates_drive_upload_on_db_failure(
    app_db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client = FakeDriveClient()
    upload = _make_upload(tmp_path)
    data = ItemCreate(name="Broken Item", shop_name="Shop")

    def _boom() -> None:
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(app_db_session, "commit", _boom)

    with pytest.raises(RuntimeError):
        item_service.create_item_with_file(app_db_session, data=data, primary_upload=upload, drive_client=fake_client)

    remaining_files = [f for f in fake_client._files.values() if f.mime_type != _FOLDER_MIME_TYPE]
    assert remaining_files == []  # compensating delete removed the orphaned Drive upload

    items = app_db_session.execute(select(Item).where(Item.name == "Broken Item")).scalars().all()
    assert items == []


def test_create_item_with_file_leaves_no_drive_orphan_on_upload_failure(
    app_db_session: Session, tmp_path: Path
) -> None:
    fake_client = FakeDriveClient()
    upload = _make_upload(tmp_path)
    upload.path.unlink()  # make the local file vanish so FakeDriveClient.upload_file fails
    data = ItemCreate(name="Never Uploaded", shop_name="Shop")

    with pytest.raises(Exception):
        item_service.create_item_with_file(app_db_session, data=data, primary_upload=upload, drive_client=fake_client)

    items = app_db_session.execute(select(Item).where(Item.name == "Never Uploaded")).scalars().all()
    assert items == []
