from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.drive import folder_layout
from app.drive.fake_drive_client import FakeDriveClient
from app.models.item import Item
from app.schemas.item import ItemCreate
from app.services import drive_reconcile_service, item_service, oauth_service
from app.services.upload_service import ValidatedUpload

_PAST_GRACE_PERIOD = drive_reconcile_service._BROKEN_REFERENCE_GRACE_PERIOD + timedelta(minutes=1)


def _backdate_sync(session: Session, item: Item) -> None:
    """Push a file's synced_at outside the broken-reference grace period, so
    reconcile() will actually consider it eligible for removal."""
    for file in item.files:
        file.synced_at = datetime.now(timezone.utc) - _PAST_GRACE_PERIOD
    session.commit()


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
    _backdate_sync(app_db_session, item)  # outrun the broken-reference grace period

    # Simulate a manual delete directly in Drive, bypassing the app.
    del fake_client._files[drive_file_id]

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.removed_broken_files == 1
    assert result.imported_items == 0
    refreshed = app_db_session.get(Item, created.id)
    assert refreshed is not None  # the item itself survives
    assert refreshed.files == []  # just the stale file reference is gone


def test_reconcile_does_not_remove_recently_synced_broken_reference(
    app_db_session: Session, tmp_path: Path
) -> None:
    # A file synced moments ago must survive even if it fails the Drive
    # existence check right now -- a fresh import (or a transient Drive
    # hiccup right after one) shouldn't be able to nuke the DB's only
    # reference to a file that's still sitting untouched on Drive.
    fake_client = FakeDriveClient()
    created = item_service.create_item_with_file(
        app_db_session,
        data=ItemCreate(name="Just Synced", shop_name="Shop"),
        primary_upload=_make_upload(tmp_path, "asset.zip"),
        drive_client=fake_client,
    )
    item = app_db_session.get(Item, created.id)
    drive_file_id = item.files[0].drive_file_id
    del fake_client._files[drive_file_id]  # would look "broken" if checked right now

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.removed_broken_files == 0
    refreshed = app_db_session.get(Item, created.id)
    assert len(refreshed.files) == 1


def test_reconcile_leaves_pending_unsynced_item_alone(app_db_session: Session, tmp_path: Path) -> None:
    # Not yet pushed to Drive by upload_sync_service (no drive_client given
    # at creation) -- reconcile must not treat this as a broken reference
    # (it has no drive_file_id to check) or try to migrate its folder (it
    # has no drive_folder_id either).
    created = item_service.create_item_with_file(
        app_db_session,
        data=ItemCreate(name="Still Pending", shop_name="Shop"),
        primary_upload=_make_upload(tmp_path, "pending-asset.zip"),
    )
    fake_client = FakeDriveClient()

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.removed_broken_files == 0
    assert result.migrated_files == 0
    refreshed = app_db_session.get(Item, created.id)
    assert len(refreshed.files) == 1
    assert refreshed.files[0].synced_at is None


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
    assert result.migrated_files == 0
    item = app_db_session.get(Item, created.id)
    assert len(item.files) == 1


def test_reconcile_imports_file_added_directly_to_upload_folder(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    upload_id = folder_layout.ensure_upload_folder(fake_client)
    asset_path = tmp_path / "outfit.zip"
    asset_path.write_bytes(b"zip bytes")
    fake_client.upload_file(local_path=asset_path, name="outfit.zip", parent_id=upload_id, mime_type="application/zip")

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.removed_broken_files == 0
    assert result.imported_items == 1
    items = app_db_session.execute(select(Item)).scalars().all()
    assert len(items) == 1
    assert items[0].name == "outfit"
    assert items[0].shop.name == "未設定"
    assert items[0].avatars == []
    assert items[0].files[0].file_role.value == "primary"

    # imported file is moved out of upload/ into the flat file/ folder
    file_folder_id = folder_layout.ensure_file_folder(fake_client)
    assert items[0].files[0].drive_folder_id == file_folder_id


def test_reconcile_imports_orphan_file_left_directly_in_file_folder(app_db_session: Session, tmp_path: Path) -> None:
    """Covers item_service's "MANUAL CLEANUP NEEDED" failure mode: a Drive
    upload succeeded but the follow-up DB write (and its compensating Drive
    delete) failed, leaving a file in `file/` with no DB record and no way
    for the app to show it. Reconcile must self-heal this into a visible item.
    """
    fake_client = FakeDriveClient()
    file_folder_id = folder_layout.ensure_file_folder(fake_client)
    asset_path = tmp_path / "orphaned.zip"
    asset_path.write_bytes(b"zip bytes")
    fake_client.upload_file(local_path=asset_path, name="orphaned.zip", parent_id=file_folder_id, mime_type="application/zip")

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.imported_items == 1
    items = app_db_session.execute(select(Item)).scalars().all()
    assert len(items) == 1
    assert items[0].name == "orphaned"
    assert items[0].files[0].drive_folder_id == file_folder_id


def test_reconcile_import_of_one_file_surviving_another_files_failure(
    app_db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One file blowing up in an unexpected way (not just a Drive move
    # failure, which _move_file already tolerates) must not discard every
    # other file already successfully imported in the same sweep.
    fake_client = FakeDriveClient()
    upload_id = folder_layout.ensure_upload_folder(fake_client)
    file_id = folder_layout.ensure_file_folder(fake_client)
    for name in ("good.zip", "poison.zip"):
        path = tmp_path / name
        path.write_bytes(b"zip bytes")
        fake_client.upload_file(local_path=path, name=name, parent_id=upload_id, mime_type="application/zip")

    real_build_item_file = drive_reconcile_service._build_item_file

    def _flaky_build_item_file(item_id, role, drive_file, folder_id):
        if drive_file.name == "poison.zip":
            raise RuntimeError("boom")
        return real_build_item_file(item_id, role, drive_file, folder_id)

    monkeypatch.setattr(drive_reconcile_service, "_build_item_file", _flaky_build_item_file)

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.imported_items == 1
    items = app_db_session.execute(select(Item)).scalars().all()
    assert [i.name for i in items] == ["good"]
    # The Drive move for poison.zip already succeeded before its DB write
    # failed (moves now happen before the transaction, not inside it -- see
    # _move_file), so it ends up sitting in file/ with no DB record, same as
    # any other Drive-succeeded-DB-failed orphan -- self-heals on a later
    # sweep instead of being silently lost. See the module docstring.
    assert "poison.zip" not in {f.name for f in fake_client.list_folder(upload_id)}
    assert "poison.zip" in {f.name for f in fake_client.list_folder(file_id)}


def test_reconcile_pairs_sibling_image_as_thumbnail(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    upload_id = folder_layout.ensure_upload_folder(fake_client)
    asset_path = tmp_path / "thing.unitypackage"
    asset_path.write_bytes(b"pkg bytes")
    thumb_path = tmp_path / "thing.png"
    thumb_path.write_bytes(b"\x89PNG\r\n")
    fake_client.upload_file(local_path=asset_path, name="thing.unitypackage", parent_id=upload_id)
    fake_client.upload_file(local_path=thumb_path, name="thing.png", parent_id=upload_id, mime_type="image/png")

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.imported_items == 1
    item = app_db_session.execute(select(Item)).scalars().one()
    roles = {f.file_role.value for f in item.files}
    assert roles == {"primary", "thumbnail"}


def test_reconcile_skips_orphan_image_with_no_asset_file(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    upload_id = folder_layout.ensure_upload_folder(fake_client)
    thumb_path = tmp_path / "lonely.png"
    thumb_path.write_bytes(b"\x89PNG\r\n")
    fake_client.upload_file(local_path=thumb_path, name="lonely.png", parent_id=upload_id, mime_type="image/png")

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.imported_items == 0
    assert app_db_session.execute(select(Item)).scalars().all() == []


def test_reconcile_ignores_files_outside_allowed_extensions(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    upload_id = folder_layout.ensure_upload_folder(fake_client)
    stray_path = tmp_path / "notes.txt"
    stray_path.write_bytes(b"just some notes")
    fake_client.upload_file(local_path=stray_path, name="notes.txt", parent_id=upload_id)

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.imported_items == 0
    assert app_db_session.execute(select(Item)).scalars().all() == []


def test_reconcile_migrates_legacy_nested_file_into_flat_file_folder(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    created = item_service.create_item_with_file(
        app_db_session,
        data=ItemCreate(name="Legacy Item", shop_name="Shop"),
        primary_upload=_make_upload(tmp_path, "asset.zip"),
        drive_client=fake_client,
    )
    item = app_db_session.get(Item, created.id)
    stored_file = item.files[0]

    # Simulate the pre-restructure nested layout by re-parenting the file
    # directly in Drive and pointing the DB record at that old folder.
    root_id = fake_client.get_or_create_folder(folder_layout.ROOT_FOLDER_NAME)
    legacy_folder_id = fake_client.get_or_create_folder("Manuka", root_id)
    fake_client.move_file(file_id=stored_file.drive_file_id, new_parent_id=legacy_folder_id, old_parent_id=stored_file.drive_folder_id)
    stored_file.drive_folder_id = legacy_folder_id
    app_db_session.commit()

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.migrated_files == 1
    assert result.removed_broken_files == 0
    file_folder_id = folder_layout.ensure_file_folder(fake_client)
    refreshed = app_db_session.get(Item, created.id)
    assert refreshed.files[0].drive_folder_id == file_folder_id
    assert fake_client.get_metadata(refreshed.files[0].drive_file_id).parent_id == file_folder_id


def test_reconcile_regenerates_root_folder_if_deleted(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    root_id = folder_layout.ensure_folder_path(fake_client, folder_layout.ROOT_FOLDER_NAME)
    del fake_client._files[root_id]  # simulate deleting the whole root folder directly in Drive

    result = drive_reconcile_service.reconcile(app_db_session, fake_client)

    assert result.removed_broken_files == 0
    assert result.imported_items == 0
    new_root_id = fake_client.get_or_create_folder(folder_layout.ROOT_FOLDER_NAME)
    assert new_root_id != root_id  # a fresh folder was created, not the deleted one


def test_reconcile_now_blocking_imports_file_dropped_directly_into_drive(
    app_db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Covers the periodic background sweep (reconcile_loop) rather than the
    # manual /settings button: a file dropped straight into Drive's upload/
    # folder should turn into a visible item on its own, the same way an
    # in-app upload does, without anyone clicking "reconcile now".
    fake_client = FakeDriveClient()
    upload_id = folder_layout.ensure_upload_folder(fake_client)
    asset_path = tmp_path / "auto-picked-up.zip"
    asset_path.write_bytes(b"zip bytes")
    fake_client.upload_file(local_path=asset_path, name="auto-picked-up.zip", parent_id=upload_id, mime_type="application/zip")
    monkeypatch.setattr(oauth_service, "make_drive_client", lambda db: fake_client)

    drive_reconcile_service._reconcile_now_blocking()

    items = app_db_session.execute(select(Item)).scalars().all()
    assert len(items) == 1
    assert items[0].name == "auto-picked-up"


def test_reconcile_now_blocking_is_a_noop_when_drive_not_connected(
    app_db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise_not_connected(db):
        raise oauth_service.NotConnectedError()

    monkeypatch.setattr(oauth_service, "make_drive_client", _raise_not_connected)

    drive_reconcile_service._reconcile_now_blocking()  # must not raise

    assert app_db_session.execute(select(Item)).scalars().all() == []
