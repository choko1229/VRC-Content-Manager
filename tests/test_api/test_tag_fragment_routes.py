"""Coverage for /fragments/tags/*: the settings page's tag management table
(see app/web/fragments/tags.py, app/templates/partials/tag_table.html)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.main import app
from app.schemas.item import ItemCreate
from app.services import item_service, tag_service
from app.services.upload_service import ValidatedUpload


@pytest.fixture()
def client(app_db_session: Session):
    def override_get_db():
        yield app_db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client, app_db_session
    finally:
        app.dependency_overrides.pop(get_db, None)


def _csrf_token(test_client: TestClient) -> str:
    page = test_client.get("/settings")
    match = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


def _make_upload(tmp_path: Path, name: str = "item.unitypackage") -> ValidatedUpload:
    path = tmp_path / name
    path.write_bytes(b"dummy")
    return ValidatedUpload(
        path=path, original_filename=name, size_bytes=5, content_type="application/octet-stream", extension=".unitypackage"
    )


def test_rename_tag_updates_name_and_returns_table(client, tmp_path: Path) -> None:
    test_client, db = client
    item_service.create_item_with_file(
        db, data=ItemCreate(name="Item", shop_name="Shop", tags=["旧名"]), primary_upload=_make_upload(tmp_path)
    )
    tag_id = next(t.id for t in tag_service.list_tags(db) if t.name == "旧名")
    token = _csrf_token(test_client)

    response = test_client.post(
        f"/fragments/tags/{tag_id}/rename", headers={"X-CSRF-Token": token}, data={"name": "新名"}
    )

    assert response.status_code == 200
    assert "新名" in response.text
    assert "旧名" not in response.text


def test_rename_tag_to_existing_name_merges_and_shows_combined_count(client, tmp_path: Path) -> None:
    test_client, db = client
    item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Item A", shop_name="Shop", tags=["水着"]),
        primary_upload=_make_upload(tmp_path, "a.unitypackage"),
    )
    item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Item B", shop_name="Shop", tags=["みずぎ"]),
        primary_upload=_make_upload(tmp_path, "b.unitypackage"),
    )
    typo_id = next(t.id for t in tag_service.list_tags(db) if t.name == "みずぎ")
    token = _csrf_token(test_client)

    response = test_client.post(
        f"/fragments/tags/{typo_id}/rename", headers={"X-CSRF-Token": token}, data={"name": "水着"}
    )

    assert response.status_code == 200
    tags = tag_service.list_tags(db)
    assert [t.name for t in tags] == ["水着"]
    assert tags[0].item_count == 2


def test_rename_tag_rejects_blank_name(client, tmp_path: Path) -> None:
    test_client, db = client
    item_service.create_item_with_file(
        db, data=ItemCreate(name="Item", shop_name="Shop", tags=["タグ"]), primary_upload=_make_upload(tmp_path)
    )
    tag_id = next(t.id for t in tag_service.list_tags(db) if t.name == "タグ")
    token = _csrf_token(test_client)

    response = test_client.post(
        f"/fragments/tags/{tag_id}/rename", headers={"X-CSRF-Token": token}, data={"name": "  "}
    )

    # 200, not 422: htmx never swaps a 4xx response into the DOM by default
    # (see app/web/fragments/tags.py's _table_response), so the error text
    # has to arrive in a 200 response to actually reach the user.
    assert response.status_code == 200
    assert "タグ名を入力してください" in response.text
    assert tag_service.get_tag(db, tag_id).name == "タグ"  # unchanged


def test_rename_tag_without_csrf_token_is_rejected(client, tmp_path: Path) -> None:
    test_client, db = client
    item_service.create_item_with_file(
        db, data=ItemCreate(name="Item", shop_name="Shop", tags=["タグ"]), primary_upload=_make_upload(tmp_path)
    )
    tag_id = next(t.id for t in tag_service.list_tags(db) if t.name == "タグ")

    response = test_client.post(f"/fragments/tags/{tag_id}/rename", data={"name": "別名"})

    assert response.status_code == 403
    assert tag_service.get_tag(db, tag_id).name == "タグ"  # unchanged


def test_delete_tag_removes_it(client, tmp_path: Path) -> None:
    test_client, db = client
    item_service.create_item_with_file(
        db, data=ItemCreate(name="Item", shop_name="Shop", tags=["消す"]), primary_upload=_make_upload(tmp_path)
    )
    tag_id = next(t.id for t in tag_service.list_tags(db) if t.name == "消す")
    token = _csrf_token(test_client)

    response = test_client.delete(f"/fragments/tags/{tag_id}", headers={"X-CSRF-Token": token})

    assert response.status_code == 200
    assert tag_service.list_tags(db) == []


def test_rename_unknown_tag_shows_error(client) -> None:
    test_client, _db = client
    token = _csrf_token(test_client)

    response = test_client.post(
        "/fragments/tags/999/rename", headers={"X-CSRF-Token": token}, data={"name": "何か"}
    )

    # 200, not 404: htmx never swaps a 4xx response into the DOM by default.
    assert response.status_code == 200
    assert "対象のタグが見つかりません" in response.text
