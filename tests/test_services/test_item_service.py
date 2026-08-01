from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.drive.fake_drive_client import _FOLDER_MIME_TYPE, FakeDriveClient
from app.models.item import Item
from app.models.item_file import FileRole
from app.models.license import TriState
from app.schemas.item import ItemCreate, ItemSearchFilters, ItemUpdate
from app.services import drive_sync_service, item_service, shop_service, thumbnail_service
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


def _create_item(
    db: Session,
    tmp_path: Path,
    *,
    name: str,
    shop_name: str = "Shop",
    tags: list[str] | None = None,
    avatars: list[str] | None = None,
    is_favorite: bool = False,
    memo: str | None = None,
    status_code: str | None = None,
):
    fake_client = FakeDriveClient()
    upload = _make_upload(tmp_path, name=f"{name}.unitypackage")
    data = ItemCreate(
        name=name,
        shop_name=shop_name,
        tags=tags or [],
        avatars=avatars or [],
        is_favorite=is_favorite,
        memo=memo,
        status_code=status_code,
    )
    return item_service.create_item_with_file(db, data=data, primary_upload=upload, drive_client=fake_client)


def test_search_items_filters_by_keyword(app_db_session: Session, tmp_path: Path) -> None:
    _create_item(app_db_session, tmp_path, name="Cool Manuka Outfit")
    _create_item(app_db_session, tmp_path, name="Something Else", memo="mentions manuka in memo")
    _create_item(app_db_session, tmp_path, name="Unrelated Item")

    results = item_service.search_items(app_db_session, ItemSearchFilters(keyword="manuka"))

    names = {r.name for r in results}
    assert names == {"Cool Manuka Outfit", "Something Else"}


def test_search_items_filters_by_tag_and_avatar(app_db_session: Session, tmp_path: Path) -> None:
    _create_item(app_db_session, tmp_path, name="A", tags=["衣装"], avatars=["Manuka"])
    _create_item(app_db_session, tmp_path, name="B", tags=["衣装"], avatars=["Raptor"])
    _create_item(app_db_session, tmp_path, name="C", tags=["ヘアー"], avatars=["Manuka"])

    by_tag = item_service.search_items(app_db_session, ItemSearchFilters(tags=["衣装"]))
    assert {r.name for r in by_tag} == {"A", "B"}

    by_avatar = item_service.search_items(app_db_session, ItemSearchFilters(avatars=["Manuka"]))
    assert {r.name for r in by_avatar} == {"A", "C"}

    by_both = item_service.search_items(app_db_session, ItemSearchFilters(tags=["衣装"], avatars=["Manuka"]))
    assert {r.name for r in by_both} == {"A"}


def test_search_items_filters_by_shop_status_and_favorite(app_db_session: Session, tmp_path: Path) -> None:
    a = _create_item(app_db_session, tmp_path, name="A", shop_name="ShopA", is_favorite=True, status_code="in_use")
    _create_item(app_db_session, tmp_path, name="B", shop_name="ShopB", is_favorite=False, status_code="imported")

    shop_a_id = app_db_session.get(Item, a.id).shop_id

    by_shop = item_service.search_items(app_db_session, ItemSearchFilters(shop_id=shop_a_id))
    assert {r.name for r in by_shop} == {"A"}

    by_status = item_service.search_items(app_db_session, ItemSearchFilters(status_code="imported"))
    assert {r.name for r in by_status} == {"B"}

    favorites = item_service.search_items(app_db_session, ItemSearchFilters(favorites_only=True))
    assert {r.name for r in favorites} == {"A"}


def test_get_item_detail_includes_license_and_history(app_db_session: Session, tmp_path: Path) -> None:
    created = _create_item(app_db_session, tmp_path, name="Detail Target", tags=["a"], avatars=["b"])

    detail = item_service.get_item_detail(app_db_session, created.id)

    assert detail.name == "Detail Target"
    assert detail.commercial_use == TriState.UNKNOWN
    assert detail.update_history == []


def test_get_item_detail_raises_not_found_for_missing_item(app_db_session: Session) -> None:
    with pytest.raises(NotFoundError):
        item_service.get_item_detail(app_db_session, 999)


def test_update_item_changes_metadata_and_associations(app_db_session: Session, tmp_path: Path) -> None:
    created = _create_item(app_db_session, tmp_path, name="Original Name", shop_name="Old Shop", tags=["old"])

    updated = item_service.update_item(
        app_db_session,
        created.id,
        ItemUpdate(
            name="New Name",
            shop_name="New Shop",
            tags=["new", "tags"],
            avatars=["Avatar1"],
            commercial_use=TriState.YES,
        ),
    )

    assert updated.name == "New Name"
    assert updated.shop_name == "New Shop"
    assert sorted(updated.tags) == ["new", "tags"]
    assert updated.avatars == ["Avatar1"]

    detail = item_service.get_item_detail(app_db_session, created.id)
    assert detail.commercial_use == TriState.YES


def test_update_item_raises_not_found_for_missing_item(app_db_session: Session) -> None:
    with pytest.raises(NotFoundError):
        item_service.update_item(app_db_session, 999, ItemUpdate(name="X", shop_name="Y"))


def test_update_item_attaches_explicit_thumbnail_upload(app_db_session: Session, tmp_path: Path) -> None:
    created = _create_item(app_db_session, tmp_path, name="No Thumb Yet")
    fake_client = FakeDriveClient()
    thumbnail = _make_upload(tmp_path, name="thumb.png", content=b"\x89PNG\r\n", extension=".png")

    updated = item_service.update_item(
        app_db_session,
        created.id,
        ItemUpdate(name="No Thumb Yet", shop_name="Shop"),
        thumbnail_upload=thumbnail,
        drive_client=fake_client,
    )

    assert updated.has_thumbnail is True
    item = app_db_session.get(Item, created.id)
    assert item.thumbnail_file is not None
    assert item.thumbnail_file.drive_folder_id == item.primary_file.drive_folder_id


def test_update_item_replaces_existing_thumbnail_and_deletes_old_drive_file(
    app_db_session: Session, tmp_path: Path
) -> None:
    fake_client = FakeDriveClient()
    upload = _make_upload(tmp_path)
    first_thumb = _make_upload(tmp_path, name="old.png", content=b"\x89PNG\r\n", extension=".png")
    data = ItemCreate(name="Has Thumb", shop_name="Shop")
    created = item_service.create_item_with_file(
        app_db_session, data=data, primary_upload=upload, thumbnail_upload=first_thumb, drive_client=fake_client
    )
    old_drive_file_id = app_db_session.get(Item, created.id).thumbnail_file.drive_file_id

    new_thumb = _make_upload(tmp_path, name="new.png", content=b"\x89PNG\r\n", extension=".png")
    item_service.update_item(
        app_db_session,
        created.id,
        ItemUpdate(name="Has Thumb", shop_name="Shop"),
        thumbnail_upload=new_thumb,
        drive_client=fake_client,
    )

    item = app_db_session.get(Item, created.id)
    assert item.thumbnail_file.original_filename == "new.png"
    assert old_drive_file_id not in fake_client._files


def test_update_item_auto_fetches_thumbnail_from_product_url_when_none_exists(
    app_db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _create_item(app_db_session, tmp_path, name="Fetch Me")
    fake_client = FakeDriveClient()

    fetched = thumbnail_service.FetchedThumbnail(content=b"\x89PNG\r\n", content_type="image/png")
    monkeypatch.setattr(thumbnail_service, "try_fetch_thumbnail", lambda url: fetched)

    updated = item_service.update_item(
        app_db_session,
        created.id,
        ItemUpdate(name="Fetch Me", shop_name="Shop", product_url="https://booth.example/items/1"),
        drive_client=fake_client,
    )

    assert updated.has_thumbnail is True


def test_update_item_does_not_overwrite_existing_thumbnail_via_auto_fetch(
    app_db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client = FakeDriveClient()
    upload = _make_upload(tmp_path)
    thumb = _make_upload(tmp_path, name="keep.png", content=b"\x89PNG\r\n", extension=".png")
    data = ItemCreate(name="Already Has One", shop_name="Shop")
    created = item_service.create_item_with_file(
        app_db_session, data=data, primary_upload=upload, thumbnail_upload=thumb, drive_client=fake_client
    )

    def _boom(url: str) -> None:
        raise AssertionError("should not fetch when item already has a thumbnail")

    monkeypatch.setattr(thumbnail_service, "try_fetch_thumbnail", _boom)

    item_service.update_item(
        app_db_session,
        created.id,
        ItemUpdate(name="Already Has One", shop_name="Shop", product_url="https://booth.example/items/2"),
        drive_client=fake_client,
    )

    item = app_db_session.get(Item, created.id)
    assert item.thumbnail_file.original_filename == "keep.png"


def test_update_item_metadata_still_saves_when_thumbnail_upload_fails(
    app_db_session: Session, tmp_path: Path
) -> None:
    created = _create_item(app_db_session, tmp_path, name="Old Name")
    thumbnail = _make_upload(tmp_path, name="thumb.png", content=b"\x89PNG\r\n", extension=".png")
    thumbnail.path.unlink()  # make the local file vanish so FakeDriveClient.upload_file fails

    updated = item_service.update_item(
        app_db_session,
        created.id,
        ItemUpdate(name="New Name", shop_name="Shop"),
        thumbnail_upload=thumbnail,
        drive_client=FakeDriveClient(),
    )

    assert updated.name == "New Name"
    assert updated.has_thumbnail is False


def test_add_update_check_records_history_entry(app_db_session: Session, tmp_path: Path) -> None:
    created = _create_item(app_db_session, tmp_path, name="Tracked Item")

    item_service.add_update_check(app_db_session, created.id, "v2.0が公開された")
    item_service.add_update_check(app_db_session, created.id, None)

    detail = item_service.get_item_detail(app_db_session, created.id)
    assert len(detail.update_history) == 2
    notes = {h.note for h in detail.update_history}
    assert notes == {"v2.0が公開された", None}


def test_add_update_check_raises_not_found_for_missing_item(app_db_session: Session) -> None:
    with pytest.raises(NotFoundError):
        item_service.add_update_check(app_db_session, 999, "note")


def test_delete_item_removes_row_and_drive_files(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    upload = _make_upload(tmp_path)
    thumb = _make_upload(tmp_path, name="thumb.png", content=b"\x89PNG\r\n", extension=".png")
    created = item_service.create_item_with_file(
        app_db_session, data=ItemCreate(name="Doomed", shop_name="Shop"), primary_upload=upload,
        thumbnail_upload=thumb, drive_client=fake_client,
    )
    item = app_db_session.get(Item, created.id)
    drive_file_ids = [f.drive_file_id for f in item.files]
    assert len(drive_file_ids) == 2

    item_service.delete_item(app_db_session, created.id, drive_client=fake_client)

    assert app_db_session.get(Item, created.id) is None
    for file_id in drive_file_ids:
        assert file_id not in fake_client._files


def test_delete_item_still_deletes_row_when_drive_cleanup_fails(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    upload = _make_upload(tmp_path)
    created = item_service.create_item_with_file(
        app_db_session, data=ItemCreate(name="Doomed 2", shop_name="Shop"), primary_upload=upload, drive_client=fake_client
    )
    item = app_db_session.get(Item, created.id)
    # Remove the file from the fake Drive out-of-band so delete_file() fails.
    del fake_client._files[item.files[0].drive_file_id]

    item_service.delete_item(app_db_session, created.id, drive_client=fake_client)

    assert app_db_session.get(Item, created.id) is None


def test_delete_item_raises_not_found_for_missing_item(app_db_session: Session) -> None:
    with pytest.raises(NotFoundError):
        item_service.delete_item(app_db_session, 999)
