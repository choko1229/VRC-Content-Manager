from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.drive.fake_drive_client import FakeDriveClient
from app.schemas.item import ItemCreate
from app.services import drive_sync_service, integrity_service, item_service
from app.services.upload_service import ValidatedUpload


@pytest.fixture(autouse=True)
def _reset_dirty(app_db_session: Session):
    drive_sync_service._dirty = False
    yield
    drive_sync_service._dirty = False


def _make_upload(tmp_path: Path, name: str = "item.vrm") -> ValidatedUpload:
    path = tmp_path / name
    path.write_bytes(b"content")
    return ValidatedUpload(path=path, original_filename=name, size_bytes=7, content_type="model/vrm", extension=".vrm")


def test_check_for_broken_references_finds_nothing_when_all_files_present(
    app_db_session: Session, tmp_path: Path
) -> None:
    fake_client = FakeDriveClient()
    item_service.create_item_with_file(
        app_db_session,
        data=ItemCreate(name="Healthy Item", shop_name="Shop"),
        primary_upload=_make_upload(tmp_path),
        drive_client=fake_client,
    )

    broken = integrity_service.check_for_broken_references(app_db_session, fake_client)

    assert broken == []


def test_check_for_broken_references_detects_missing_drive_file(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    created = item_service.create_item_with_file(
        app_db_session,
        data=ItemCreate(name="Broken Reference Item", shop_name="Shop"),
        primary_upload=_make_upload(tmp_path),
        drive_client=fake_client,
    )
    from app.models.item import Item

    item = app_db_session.get(Item, created.id)
    drive_file_id = item.files[0].drive_file_id
    fake_client.delete_file(drive_file_id)  # simulate the Drive file having vanished

    broken = integrity_service.check_for_broken_references(app_db_session, fake_client)

    assert len(broken) == 1
    assert broken[0].item_id == created.id
    assert broken[0].drive_file_id == drive_file_id
