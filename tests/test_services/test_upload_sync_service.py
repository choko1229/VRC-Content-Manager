from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.drive.fake_drive_client import FakeDriveClient
from app.models.item import Item
from app.models.item_file import ItemFile
from app.schemas.item import ItemCreate
from app.services import item_service, local_cache_service, oauth_service, upload_sync_service
from app.services.upload_service import ValidatedUpload


def _make_upload(tmp_path: Path, name: str = "asset.zip", content: bytes = b"dummy") -> ValidatedUpload:
    path = tmp_path / name
    path.write_bytes(content)
    return ValidatedUpload(
        path=path, original_filename=name, size_bytes=len(content), content_type="application/zip", extension=".zip"
    )


def _create_pending_item(db: Session, tmp_path: Path, name: str = "Pending Item") -> Item:
    result = item_service.create_item_with_file(
        db, data=ItemCreate(name=name, shop_name="Shop"), primary_upload=_make_upload(tmp_path, f"{name}.zip")
    )
    return db.get(Item, result.id)


def test_sync_item_file_pushes_to_drive_and_clears_local_cache(app_db_session: Session, tmp_path: Path) -> None:
    item = _create_pending_item(app_db_session, tmp_path)
    file = item.files[0]
    cache_path = local_cache_service.pending_upload_path(file.stored_filename)
    assert cache_path.exists()
    fake_client = FakeDriveClient()

    synced = upload_sync_service.sync_item_file(app_db_session, file.id, fake_client)

    assert synced is True
    app_db_session.refresh(file)
    assert file.synced_at is not None
    assert file.drive_file_id is not None
    assert not cache_path.exists()  # moved to Drive, local pending copy cleaned up


def test_sync_item_file_is_a_noop_for_already_synced_file(app_db_session: Session, tmp_path: Path) -> None:
    item = _create_pending_item(app_db_session, tmp_path)
    file = item.files[0]
    fake_client = FakeDriveClient()
    upload_sync_service.sync_item_file(app_db_session, file.id, fake_client)
    app_db_session.refresh(file)
    first_drive_id = file.drive_file_id

    synced_again = upload_sync_service.sync_item_file(app_db_session, file.id, fake_client)

    assert synced_again is False
    app_db_session.refresh(file)
    assert file.drive_file_id == first_drive_id


def test_sync_item_file_drops_reference_when_cache_file_missing(app_db_session: Session, tmp_path: Path) -> None:
    item = _create_pending_item(app_db_session, tmp_path)
    file = item.files[0]
    file_id = file.id
    local_cache_service.pending_upload_path(file.stored_filename).unlink()
    fake_client = FakeDriveClient()

    synced = upload_sync_service.sync_item_file(app_db_session, file_id, fake_client)

    assert synced is False
    assert app_db_session.get(ItemFile, file_id) is None


def test_sync_pending_now_syncs_all_pending_files(app_db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item_a = _create_pending_item(app_db_session, tmp_path, "Item A")
    item_b = _create_pending_item(app_db_session, tmp_path, "Item B")
    fake_client = FakeDriveClient()
    monkeypatch.setattr(oauth_service, "make_drive_client", lambda db: fake_client)

    synced_count = upload_sync_service.sync_pending_now()

    assert synced_count == 2
    for item in (item_a, item_b):
        app_db_session.refresh(item.files[0])
        assert item.files[0].synced_at is not None


def test_sync_pending_now_is_a_noop_when_drive_not_connected(
    app_db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_pending_item(app_db_session, tmp_path)

    def _raise_not_connected(db):
        raise oauth_service.NotConnectedError()

    monkeypatch.setattr(oauth_service, "make_drive_client", _raise_not_connected)

    synced_count = upload_sync_service.sync_pending_now()

    assert synced_count == 0
    items = app_db_session.execute(select(Item)).scalars().all()
    assert items[0].files[0].synced_at is None  # still pending, no crash
