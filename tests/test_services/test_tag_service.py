from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, ValidationError
from app.services import item_service, tag_service
from app.services.upload_service import ValidatedUpload
from app.schemas.item import ItemCreate
from pathlib import Path


def _make_upload(tmp_path: Path, name: str = "item.unitypackage") -> ValidatedUpload:
    path = tmp_path / name
    path.write_bytes(b"dummy")
    return ValidatedUpload(
        path=path, original_filename=name, size_bytes=5, content_type="application/octet-stream", extension=".unitypackage"
    )


# get_or_create_tags / list_tag_names predate the tag management screen
# (list_tags/rename_or_merge_tag/delete_tag below) and are still load-bearing
# in production (item creation/update, and the tag combobox's options) --
# kept here rather than dropped when this file's coverage was extended.
def test_get_or_create_tags_dedupes_and_creates(db_session: Session) -> None:
    tags = tag_service.get_or_create_tags(db_session, ["衣装", "衣装", " ヘアー ", ""])

    names = sorted(t.name for t in tags)
    assert names == ["ヘアー", "衣装"]


def test_get_or_create_tags_reuses_existing(db_session: Session) -> None:
    first = tag_service.get_or_create_tags(db_session, ["shader"])
    db_session.commit()

    second = tag_service.get_or_create_tags(db_session, ["shader"])

    assert first[0].id == second[0].id


def test_get_or_create_tags_empty_input_returns_empty(db_session: Session) -> None:
    assert tag_service.get_or_create_tags(db_session, []) == []


def test_list_tag_names_sorted(db_session: Session) -> None:
    tag_service.get_or_create_tags(db_session, ["zeta", "alpha"])
    db_session.commit()

    assert tag_service.list_tag_names(db_session) == ["alpha", "zeta"]


def test_list_tags_reports_item_count(app_db_session: Session, tmp_path: Path) -> None:
    item_service.create_item_with_file(
        app_db_session,
        data=ItemCreate(name="Item A", shop_name="Shop", tags=["夏服", "水着"]),
        primary_upload=_make_upload(tmp_path, "a.unitypackage"),
    )
    item_service.create_item_with_file(
        app_db_session,
        data=ItemCreate(name="Item B", shop_name="Shop", tags=["夏服"]),
        primary_upload=_make_upload(tmp_path, "b.unitypackage"),
    )

    tags = {t.name: t for t in tag_service.list_tags(app_db_session)}

    assert tags["夏服"].item_count == 2
    assert tags["水着"].item_count == 1


def test_rename_or_merge_tag_renames_when_name_is_free(app_db_session: Session, tmp_path: Path) -> None:
    item_service.create_item_with_file(
        app_db_session,
        data=ItemCreate(name="Item A", shop_name="Shop", tags=["旧名"]),
        primary_upload=_make_upload(tmp_path),
    )
    tag_id = next(t.id for t in tag_service.list_tags(app_db_session) if t.name == "旧名")

    result = tag_service.rename_or_merge_tag(app_db_session, tag_id, "新名")

    assert result.name == "新名"
    assert result.item_count == 1
    names = {t.name for t in tag_service.list_tags(app_db_session)}
    assert names == {"新名"}


def test_rename_or_merge_tag_merges_into_existing_name(app_db_session: Session, tmp_path: Path) -> None:
    item_service.create_item_with_file(
        app_db_session,
        data=ItemCreate(name="Item A", shop_name="Shop", tags=["水着"]),
        primary_upload=_make_upload(tmp_path, "a.unitypackage"),
    )
    item_service.create_item_with_file(
        app_db_session,
        data=ItemCreate(name="Item B", shop_name="Shop", tags=["みずぎ"]),
        primary_upload=_make_upload(tmp_path, "b.unitypackage"),
    )
    typo_id = next(t.id for t in tag_service.list_tags(app_db_session) if t.name == "みずぎ")

    result = tag_service.rename_or_merge_tag(app_db_session, typo_id, "水着")

    assert result.name == "水着"
    assert result.item_count == 2  # both items now share the one surviving tag
    names = {t.name for t in tag_service.list_tags(app_db_session)}
    assert names == {"水着"}  # the typo tag is gone, not just emptied


def test_rename_or_merge_tag_rejects_blank_name(app_db_session: Session, tmp_path: Path) -> None:
    item_service.create_item_with_file(
        app_db_session,
        data=ItemCreate(name="Item A", shop_name="Shop", tags=["タグ"]),
        primary_upload=_make_upload(tmp_path),
    )
    tag_id = next(t.id for t in tag_service.list_tags(app_db_session) if t.name == "タグ")

    with pytest.raises(ValidationError):
        tag_service.rename_or_merge_tag(app_db_session, tag_id, "   ")


def test_rename_or_merge_tag_raises_not_found_for_unknown_id(app_db_session: Session) -> None:
    with pytest.raises(NotFoundError):
        tag_service.rename_or_merge_tag(app_db_session, 999, "何か")


def test_delete_tag_removes_it_from_items(app_db_session: Session, tmp_path: Path) -> None:
    item_service.create_item_with_file(
        app_db_session,
        data=ItemCreate(name="Item A", shop_name="Shop", tags=["消す"]),
        primary_upload=_make_upload(tmp_path),
    )
    tag_id = next(t.id for t in tag_service.list_tags(app_db_session) if t.name == "消す")

    tag_service.delete_tag(app_db_session, tag_id)

    assert tag_service.list_tags(app_db_session) == []
    with pytest.raises(NotFoundError):
        tag_service.get_tag(app_db_session, tag_id)
