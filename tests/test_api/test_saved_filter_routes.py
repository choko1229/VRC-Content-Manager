"""Coverage for /fragments/saved-filters/*: the TOP page's 保存フィルタ row
(see app/web/fragments/saved_filters.py, app/templates/items/_saved_filter_list.html)."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.main import app
from app.services import saved_filter_service


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
    page = test_client.get("/items")
    match = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


def test_create_saved_filter_shows_up_in_list(client) -> None:
    test_client, db = client
    token = _csrf_token(test_client)

    response = test_client.post(
        "/fragments/saved-filters",
        headers={"X-CSRF-Token": token},
        data={"name": "お気に入りの衣装", "query_string": "favorites_only=true&category=clothing"},
    )

    assert response.status_code == 200
    assert "お気に入りの衣装" in response.text
    assert 'href="/items?favorites_only=true&amp;category=clothing"' in response.text

    filters = saved_filter_service.list_saved_filters(db)
    assert len(filters) == 1


def test_create_saved_filter_rejects_blank_name(client) -> None:
    test_client, db = client
    token = _csrf_token(test_client)

    response = test_client.post(
        "/fragments/saved-filters", headers={"X-CSRF-Token": token}, data={"name": "  ", "query_string": "q=x"}
    )

    # 200, not 422: htmx never swaps a 4xx response into the DOM by default
    # (see app/web/fragments/saved_filters.py's _list_response), so the
    # error text has to arrive in a 200 response to actually reach the user.
    assert response.status_code == 200
    assert "名前を入力してください" in response.text
    assert saved_filter_service.list_saved_filters(db) == []


def test_applying_a_saved_filter_link_filters_the_list(client, tmp_path) -> None:
    test_client, db = client
    from app.drive.fake_drive_client import FakeDriveClient
    from app.schemas.item import ItemCreate
    from app.services import item_service
    from app.services.upload_service import ValidatedUpload

    def _make_upload(name: str) -> ValidatedUpload:
        path = tmp_path / name
        path.write_bytes(b"dummy")
        return ValidatedUpload(
            path=path, original_filename=name, size_bytes=5, content_type="application/octet-stream", extension=".zip"
        )

    item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Favorite Item", shop_name="Shop", is_favorite=True),
        primary_upload=_make_upload("fav.zip"),
        drive_client=FakeDriveClient(),
    )
    item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Other Item", shop_name="Shop", is_favorite=False),
        primary_upload=_make_upload("other.zip"),
        drive_client=FakeDriveClient(),
    )
    saved_filter_service.create_saved_filter(db, "お気に入り", "favorites_only=true")

    response = test_client.get("/items?favorites_only=true")

    assert response.status_code == 200
    assert "Favorite Item" in response.text
    assert "Other Item" not in response.text


def test_delete_saved_filter_removes_it(client) -> None:
    test_client, db = client
    created = saved_filter_service.create_saved_filter(db, "消す", "q=x")
    token = _csrf_token(test_client)

    response = test_client.delete(f"/fragments/saved-filters/{created.id}", headers={"X-CSRF-Token": token})

    assert response.status_code == 200
    assert saved_filter_service.list_saved_filters(db) == []


def test_delete_unknown_saved_filter_shows_error(client) -> None:
    test_client, _db = client
    token = _csrf_token(test_client)

    response = test_client.delete("/fragments/saved-filters/999", headers={"X-CSRF-Token": token})

    # 200, not 404: htmx never swaps a 4xx response into the DOM by default.
    assert response.status_code == 200
    assert "対象の保存フィルタが見つかりません" in response.text
