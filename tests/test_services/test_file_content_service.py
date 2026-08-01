from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import DriveError
from app.drive.fake_drive_client import FakeDriveClient
from app.models.item import Item
from app.schemas.item import ItemCreate
from app.services import file_content_service, item_service, local_cache_service, upload_sync_service
from app.services.upload_service import ValidatedUpload


def _make_upload(tmp_path: Path, name: str = "asset.zip", content: bytes = b"dummy") -> ValidatedUpload:
    path = tmp_path / name
    path.write_bytes(content)
    return ValidatedUpload(
        path=path, original_filename=name, size_bytes=len(content), content_type="application/zip", extension=".zip"
    )


def _create_item(db: Session, tmp_path: Path, name: str = "Item") -> Item:
    result = item_service.create_item_with_file(
        db, data=ItemCreate(name=name, shop_name="Shop"), primary_upload=_make_upload(tmp_path, f"{name}.zip")
    )
    return db.get(Item, result.id)


def test_resolve_local_path_serves_pending_file_from_upload_cache_without_drive(
    app_db_session: Session, tmp_path: Path
) -> None:
    item = _create_item(app_db_session, tmp_path)
    file = item.files[0]
    assert file.synced_at is None

    path = file_content_service.resolve_local_path(file, drive_client=None)

    assert path == local_cache_service.pending_upload_path(file.stored_filename)
    assert path.read_bytes() == b"dummy"


def test_resolve_local_path_fetches_from_drive_when_synced_and_not_cached(
    app_db_session: Session, tmp_path: Path
) -> None:
    item = _create_item(app_db_session, tmp_path)
    file = item.files[0]
    fake_client = FakeDriveClient()
    upload_sync_service.sync_item_file(app_db_session, file.id, fake_client)
    app_db_session.refresh(file)

    path = file_content_service.resolve_local_path(file, fake_client)

    assert path == local_cache_service.download_cache_path(file.drive_file_id)
    assert path.exists()


def test_resolve_local_path_serves_from_download_cache_without_drive_client(
    app_db_session: Session, tmp_path: Path
) -> None:
    item = _create_item(app_db_session, tmp_path)
    file = item.files[0]
    fake_client = FakeDriveClient()
    upload_sync_service.sync_item_file(app_db_session, file.id, fake_client)
    app_db_session.refresh(file)
    file_content_service.resolve_local_path(file, fake_client)  # populates the download cache

    path = file_content_service.resolve_local_path(file, drive_client=None)  # no client needed this time

    assert path.exists()


def test_resolve_local_path_raises_when_pending_cache_file_missing(app_db_session: Session, tmp_path: Path) -> None:
    item = _create_item(app_db_session, tmp_path)
    file = item.files[0]
    local_cache_service.pending_upload_path(file.stored_filename).unlink()

    with pytest.raises(DriveError):
        file_content_service.resolve_local_path(file, drive_client=None)


def test_resolve_local_path_raises_when_synced_but_no_client_and_not_cached(
    app_db_session: Session, tmp_path: Path
) -> None:
    item = _create_item(app_db_session, tmp_path)
    file = item.files[0]
    fake_client = FakeDriveClient()
    upload_sync_service.sync_item_file(app_db_session, file.id, fake_client)
    app_db_session.refresh(file)

    with pytest.raises(DriveError):
        file_content_service.resolve_local_path(file, drive_client=None)
