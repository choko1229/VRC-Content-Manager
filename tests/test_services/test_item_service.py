from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.drive import folder_layout
from app.drive.fake_drive_client import _FOLDER_MIME_TYPE, FakeDriveClient
from app.models.item import Item, ItemCategory
from app.models.item_file import FileRole
from app.models.license import TriState
from app.schemas.item import ItemCreate, ItemSearchFilters, ItemUpdate
from app.services import (
    avatar_service,
    drive_sync_service,
    file_content_service,
    item_service,
    local_cache_service,
    shop_service,
    thumbnail_service,
    upload_sync_service,
)
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
    _register_avatar(app_db_session, tmp_path, "Manuka")
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


def test_file_status_is_pending_then_synced_then_cached(app_db_session: Session, tmp_path: Path) -> None:
    upload = _make_upload(tmp_path)
    data = ItemCreate(name="Status Progression", shop_name="Shop")
    created = item_service.create_item_with_file(app_db_session, data=data, primary_upload=upload)

    detail = item_service.get_item_detail(app_db_session, created.id)
    assert detail.file_status == "pending"

    fake_client = FakeDriveClient()
    item = app_db_session.get(Item, created.id)
    upload_sync_service.sync_item_file(app_db_session, item.files[0].id, fake_client)

    detail = item_service.get_item_detail(app_db_session, created.id)
    assert detail.file_status == "synced"

    file_content_service.resolve_local_path(item.files[0], fake_client)  # populates the download cache

    detail = item_service.get_item_detail(app_db_session, created.id)
    assert detail.file_status == "cached"


def test_search_items_exposes_file_status(app_db_session: Session, tmp_path: Path) -> None:
    upload = _make_upload(tmp_path)
    item_service.create_item_with_file(
        app_db_session, data=ItemCreate(name="List Status", shop_name="Shop"), primary_upload=upload
    )

    rows = item_service.search_items(app_db_session, ItemSearchFilters())

    assert rows[0].file_status == "pending"


def test_create_item_with_file_makes_no_drive_call_without_explicit_client(
    app_db_session: Session, tmp_path: Path
) -> None:
    # The core new guarantee: create_item_with_file never touches Drive on
    # its own -- the upload is cached locally and the DB row is written
    # immediately, with synced_at left NULL for upload_sync_service to push
    # in the background. This is what keeps the request fast regardless of
    # Drive's latency (and lets uploading work even while Drive is down).
    upload = _make_upload(tmp_path)
    data = ItemCreate(name="No Drive Call", shop_name="Shop")

    result = item_service.create_item_with_file(app_db_session, data=data, primary_upload=upload)

    item = app_db_session.get(Item, result.id)
    file = item.files[0]
    assert file.synced_at is None
    assert file.drive_file_id is None
    assert local_cache_service.pending_upload_path(file.stored_filename).exists()


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


def test_create_item_with_file_rolls_back_cleanly_on_db_commit_failure(
    app_db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No Drive call happens until *after* a successful commit (see
    # test_create_item_with_file_makes_no_drive_call_without_explicit_client),
    # so there's nothing to compensate for here -- just verify the failed
    # attempt leaves no DB row and the moved-into-cache file behind it.
    fake_client = FakeDriveClient()
    upload = _make_upload(tmp_path)
    data = ItemCreate(name="Broken Item", shop_name="Shop")

    def _boom() -> None:
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(app_db_session, "commit", _boom)

    with pytest.raises(RuntimeError):
        item_service.create_item_with_file(app_db_session, data=data, primary_upload=upload, drive_client=fake_client)

    remaining_files = [f for f in fake_client._files.values() if f.mime_type != _FOLDER_MIME_TYPE]
    assert remaining_files == []  # Drive was never touched

    items = app_db_session.execute(select(Item).where(Item.name == "Broken Item")).scalars().all()
    assert items == []


def test_create_item_with_file_leaves_no_orphan_row_when_caching_fails(
    app_db_session: Session, tmp_path: Path
) -> None:
    upload = _make_upload(tmp_path)
    upload.path.unlink()  # make the source file vanish so moving it into the pending cache fails
    data = ItemCreate(name="Never Cached", shop_name="Shop")

    with pytest.raises(Exception):
        item_service.create_item_with_file(app_db_session, data=data, primary_upload=upload)

    items = app_db_session.execute(select(Item).where(Item.name == "Never Cached")).scalars().all()
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
    category: ItemCategory = ItemCategory.CLOTHING,
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
        category=category,
    )
    return item_service.create_item_with_file(db, data=data, primary_upload=upload, drive_client=fake_client)


def _register_avatar(db: Session, tmp_path: Path, name: str):
    """Avatars must be backed by an uploaded item (see avatar_service.set_item_as_avatar) --
    this uploads a throwaway base-model item and registers it under `name`."""
    base_item = _create_item(db, tmp_path, name=f"{name} base model")
    return avatar_service.set_item_as_avatar(db, base_item.id, name=name, memo=None)


def test_search_items_filters_by_keyword(app_db_session: Session, tmp_path: Path) -> None:
    _create_item(app_db_session, tmp_path, name="Cool Manuka Outfit")
    _create_item(app_db_session, tmp_path, name="Something Else", memo="mentions manuka in memo")
    _create_item(app_db_session, tmp_path, name="Unrelated Item")

    results = item_service.search_items(app_db_session, ItemSearchFilters(keyword="manuka"))

    names = {r.name for r in results}
    assert names == {"Cool Manuka Outfit", "Something Else"}


def test_search_items_filters_by_tag_and_avatar(app_db_session: Session, tmp_path: Path) -> None:
    _register_avatar(app_db_session, tmp_path, "Manuka")
    _register_avatar(app_db_session, tmp_path, "Raptor")
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


def test_new_item_defaults_to_clothing_category(app_db_session: Session, tmp_path: Path) -> None:
    # The historical implicit behavior (most items are avatar clothing/
    # accessories) stays the default for anyone not opting into a category.
    created = _create_item(app_db_session, tmp_path, name="Default Category Item")

    detail = item_service.get_item_detail(app_db_session, created.id)
    assert detail.category == ItemCategory.CLOTHING
    assert detail.category_label == "衣装・アバター素材"


def test_create_item_with_file_respects_explicit_category(app_db_session: Session, tmp_path: Path) -> None:
    created = _create_item(app_db_session, tmp_path, name="A Shader Extension", category=ItemCategory.SHADER_EXTENSION)

    detail = item_service.get_item_detail(app_db_session, created.id)
    assert detail.category == ItemCategory.SHADER_EXTENSION
    assert detail.category_label == "シェーダー拡張"


def test_update_item_can_change_category(app_db_session: Session, tmp_path: Path) -> None:
    created = _create_item(app_db_session, tmp_path, name="Reclassify Me")

    item_service.update_item(
        app_db_session,
        created.id,
        ItemUpdate(name="Reclassify Me", shop_name="Shop", category=ItemCategory.TOOL),
    )

    detail = item_service.get_item_detail(app_db_session, created.id)
    assert detail.category == ItemCategory.TOOL


def test_search_items_filters_by_category(app_db_session: Session, tmp_path: Path) -> None:
    _create_item(app_db_session, tmp_path, name="A Tool", category=ItemCategory.TOOL)
    _create_item(app_db_session, tmp_path, name="Some Clothing", category=ItemCategory.CLOTHING)

    by_category = item_service.search_items(app_db_session, ItemSearchFilters(category=ItemCategory.TOOL))

    assert {r.name for r in by_category} == {"A Tool"}


def test_update_item_changes_metadata_and_associations(app_db_session: Session, tmp_path: Path) -> None:
    _register_avatar(app_db_session, tmp_path, "Avatar1")
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
    assert item.thumbnail_file.drive_folder_id == folder_layout.ensure_file_folder(fake_client)


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


def test_delete_item_removes_local_cache_file_for_pending_item(app_db_session: Session, tmp_path: Path) -> None:
    # Never synced (no drive_client passed to create_item_with_file), so
    # the only copy is the local pending-upload cache -- deleting the item
    # must clean that up too, not try to reach Drive for it.
    upload = _make_upload(tmp_path)
    created = item_service.create_item_with_file(
        app_db_session, data=ItemCreate(name="Pending Doomed", shop_name="Shop"), primary_upload=upload
    )
    item = app_db_session.get(Item, created.id)
    cache_path = local_cache_service.pending_upload_path(item.files[0].stored_filename)
    assert cache_path.exists()

    item_service.delete_item(app_db_session, created.id)

    assert app_db_session.get(Item, created.id) is None
    assert not cache_path.exists()


def test_delete_item_forgets_download_cache_entry(app_db_session: Session, tmp_path: Path) -> None:
    fake_client = FakeDriveClient()
    upload = _make_upload(tmp_path)
    created = item_service.create_item_with_file(
        app_db_session, data=ItemCreate(name="Cached Doomed", shop_name="Shop"), primary_upload=upload,
        drive_client=fake_client,
    )
    item = app_db_session.get(Item, created.id)
    drive_file_id = item.files[0].drive_file_id
    cache_path = local_cache_service.download_cache_path(drive_file_id)
    cache_path.write_bytes(b"cached copy")

    item_service.delete_item(app_db_session, created.id, drive_client=fake_client)

    assert not cache_path.exists()


def test_bulk_update_sets_status_and_favorite_on_all_selected_items(app_db_session: Session, tmp_path: Path) -> None:
    a = _create_item(app_db_session, tmp_path, name="Bulk A", status_code="unorganized")
    b = _create_item(app_db_session, tmp_path, name="Bulk B", status_code="unorganized")
    untouched = _create_item(app_db_session, tmp_path, name="Bulk C (untouched)", status_code="unorganized")

    updated_count = item_service.bulk_update(
        app_db_session, [a.id, b.id], status_code="in_use", is_favorite=True
    )

    assert updated_count == 2
    for item_id in (a.id, b.id):
        detail = item_service.get_item_detail(app_db_session, item_id)
        assert detail.status_code == "in_use"
        assert detail.is_favorite is True

    detail_untouched = item_service.get_item_detail(app_db_session, untouched.id)
    assert detail_untouched.status_code == "unorganized"
    assert detail_untouched.is_favorite is False


def test_bulk_update_adds_tags_and_avatars_without_removing_existing(app_db_session: Session, tmp_path: Path) -> None:
    _register_avatar(app_db_session, tmp_path, "既存アバター")
    _register_avatar(app_db_session, tmp_path, "新アバター")
    a = _create_item(app_db_session, tmp_path, name="Tagged A", tags=["既存タグ"], avatars=["既存アバター"])
    b = _create_item(app_db_session, tmp_path, name="Tagged B")

    item_service.bulk_update(
        app_db_session, [a.id, b.id], add_tag_names=["新タグ"], add_avatar_names=["新アバター"]
    )

    detail_a = item_service.get_item_detail(app_db_session, a.id)
    assert sorted(detail_a.tags) == ["新タグ", "既存タグ"]
    assert sorted(detail_a.avatars) == ["新アバター", "既存アバター"]

    detail_b = item_service.get_item_detail(app_db_session, b.id)
    assert detail_b.tags == ["新タグ"]
    assert detail_b.avatars == ["新アバター"]


def test_bulk_update_leaves_unspecified_fields_untouched(app_db_session: Session, tmp_path: Path) -> None:
    created = _create_item(app_db_session, tmp_path, name="Leave Alone", status_code="imported", tags=["keep"])

    updated_count = item_service.bulk_update(app_db_session, [created.id])

    assert updated_count == 1
    detail = item_service.get_item_detail(app_db_session, created.id)
    assert detail.status_code == "imported"
    assert detail.tags == ["keep"]
    assert detail.is_favorite is False


def test_bulk_update_returns_zero_for_empty_id_list(app_db_session: Session) -> None:
    assert item_service.bulk_update(app_db_session, [], status_code="in_use") == 0


def _set_product_url(db: Session, item_id: int, url: str) -> None:
    item = db.get(Item, item_id)
    item.product_url = url
    db.commit()


def test_find_duplicate_product_url_item_finds_the_other_item(app_db_session: Session, tmp_path: Path) -> None:
    a = _create_item(app_db_session, tmp_path, name="A")
    b = _create_item(app_db_session, tmp_path, name="B")
    _set_product_url(app_db_session, a.id, "https://booth.pm/ja/items/1")
    _set_product_url(app_db_session, b.id, "https://booth.pm/ja/items/1")

    found = item_service.find_duplicate_product_url_item(
        app_db_session, "https://booth.pm/ja/items/1", exclude_item_id=a.id
    )

    assert found is not None
    assert found.id == b.id


def test_find_duplicate_product_url_item_returns_none_when_unique(app_db_session: Session, tmp_path: Path) -> None:
    a = _create_item(app_db_session, tmp_path, name="A")
    _set_product_url(app_db_session, a.id, "https://booth.pm/ja/items/1")

    found = item_service.find_duplicate_product_url_item(
        app_db_session, "https://booth.pm/ja/items/1", exclude_item_id=a.id
    )

    assert found is None


def test_merge_item_into_moves_source_file_as_attachment_and_deletes_source(
    app_db_session: Session, tmp_path: Path
) -> None:
    source = _create_item(app_db_session, tmp_path, name="Source Item")
    target = _create_item(app_db_session, tmp_path, name="Target Item")
    source_file_id = app_db_session.get(Item, source.id).files[0].id

    result = item_service.merge_item_into(app_db_session, source.id, target.id)

    assert result.id == target.id
    assert app_db_session.get(Item, source.id) is None  # source item is gone

    detail = item_service.get_item_detail(app_db_session, target.id)
    assert detail.name == "Target Item"  # target's own metadata is untouched
    assert len(detail.attachment_files) == 1
    assert detail.attachment_files[0].id == source_file_id

    target_item = app_db_session.get(Item, target.id)
    assert target_item.primary_file is not None
    assert target_item.primary_file.item_id == target.id  # target keeps its own primary


def test_merge_item_into_reparents_source_thumbnail_when_target_has_none(
    app_db_session: Session, tmp_path: Path
) -> None:
    source = _create_item(app_db_session, tmp_path, name="Source With Thumb")
    target = _create_item(app_db_session, tmp_path, name="Target No Thumb")
    thumb_upload = _make_upload(tmp_path, name="thumb.png", content=b"\x89PNG\r\n", extension=".png")
    item_service.update_item(
        app_db_session, source.id, ItemUpdate(name="Source With Thumb", shop_name="Shop"), thumbnail_upload=thumb_upload
    )
    assert item_service.get_item_detail(app_db_session, source.id).has_thumbnail is True

    item_service.merge_item_into(app_db_session, source.id, target.id)

    detail = item_service.get_item_detail(app_db_session, target.id)
    assert detail.has_thumbnail is True


def test_merge_item_into_drops_source_thumbnail_when_target_already_has_one(
    app_db_session: Session, tmp_path: Path
) -> None:
    source = _create_item(app_db_session, tmp_path, name="Source With Thumb 2")
    target = _create_item(app_db_session, tmp_path, name="Target With Thumb")
    for item_id, name in [(source.id, "Source With Thumb 2"), (target.id, "Target With Thumb")]:
        thumb_upload = _make_upload(tmp_path, name=f"thumb-{item_id}.png", content=b"\x89PNG\r\n", extension=".png")
        item_service.update_item(app_db_session, item_id, ItemUpdate(name=name, shop_name="Shop"), thumbnail_upload=thumb_upload)

    item_service.merge_item_into(app_db_session, source.id, target.id)

    target_item = app_db_session.get(Item, target.id)
    # Still exactly one thumbnail on the target -- the source's redundant one was dropped, not duplicated.
    assert sum(1 for f in target_item.files if f.file_role.value == "thumbnail") == 1


def test_merge_item_into_raises_not_found_for_missing_source_or_target(
    app_db_session: Session, tmp_path: Path
) -> None:
    only = _create_item(app_db_session, tmp_path, name="Only Item")

    with pytest.raises(NotFoundError):
        item_service.merge_item_into(app_db_session, 999, only.id)
    with pytest.raises(NotFoundError):
        item_service.merge_item_into(app_db_session, only.id, 999)


def test_auto_merge_duplicate_products_merges_all_groups_keeping_earliest(
    app_db_session: Session, tmp_path: Path
) -> None:
    keep = _create_item(app_db_session, tmp_path, name="Keep Me (earliest)")
    dup1 = _create_item(app_db_session, tmp_path, name="Duplicate 1")
    dup2 = _create_item(app_db_session, tmp_path, name="Duplicate 2")
    unrelated = _create_item(app_db_session, tmp_path, name="Unrelated")
    for item_id in (keep.id, dup1.id, dup2.id):
        _set_product_url(app_db_session, item_id, "https://booth.pm/ja/items/1")
    _set_product_url(app_db_session, unrelated.id, "https://booth.pm/ja/items/2")

    merged_count = item_service.auto_merge_duplicate_products(app_db_session)

    assert merged_count == 2
    assert app_db_session.get(Item, dup1.id) is None
    assert app_db_session.get(Item, dup2.id) is None
    assert app_db_session.get(Item, unrelated.id) is not None

    detail = item_service.get_item_detail(app_db_session, keep.id)
    assert len(detail.attachment_files) == 2


def test_auto_merge_duplicate_products_is_a_noop_when_no_duplicates(app_db_session: Session, tmp_path: Path) -> None:
    a = _create_item(app_db_session, tmp_path, name="A")
    _set_product_url(app_db_session, a.id, "https://booth.pm/ja/items/1")
    _create_item(app_db_session, tmp_path, name="B")  # no product_url at all

    assert item_service.auto_merge_duplicate_products(app_db_session) == 0
