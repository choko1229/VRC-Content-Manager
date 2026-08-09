"""Coverage for POST /fragments/items/{id}/refresh-from-booth: the edit
panel's "BOOTHの最新情報に更新" button (see app/web/fragments/items.py:
refresh_item_from_booth_fragment)."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.drive.fake_drive_client import FakeDriveClient
from app.main import app
from app.schemas.item import ItemCreate
from app.services import booth_info_service, item_service
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


def _csrf_token(page_html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page_html)
    assert match is not None
    return match.group(1)


def _make_upload(tmp_path, name: str = "asset.zip", content: bytes = b"dummy") -> ValidatedUpload:
    path = tmp_path / name
    path.write_bytes(content)
    return ValidatedUpload(
        path=path, original_filename=name, size_bytes=len(content), content_type="application/zip", extension=".zip"
    )


def test_refresh_from_booth_updates_fields_via_oob_swap(client, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, db = client
    created = item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Old Name", shop_name="Old Shop", product_url="https://booth.pm/ja/items/1"),
        primary_upload=_make_upload(tmp_path),
        drive_client=FakeDriveClient(),
    )
    info = booth_info_service.BoothProductInfo(
        name="New Name", shop_name="New Shop", shop_url=None, price=None, image_url=None, description="新しい説明"
    )
    monkeypatch.setattr(booth_info_service, "try_fetch_product_info", lambda url, **kw: info)
    token = _csrf_token(test_client.get(f"/fragments/items/{created.id}/edit").text)

    response = test_client.post(
        f"/fragments/items/{created.id}/refresh-from-booth", headers={"X-CSRF-Token": token}
    )

    assert response.status_code == 200
    assert "BOOTHの最新情報に更新しました" in response.text
    assert 'id="f-name"' in response.text and "New Name" in response.text
    assert 'id="f-shop_name"' in response.text and "New Shop" in response.text
    assert 'id="f-description"' in response.text and "新しい説明" in response.text
    assert response.headers.get("hx-trigger") == "item-saved"

    detail = item_service.get_item_detail(db, created.id)
    assert detail.name == "New Name"


def test_refresh_from_booth_shows_error_when_fetch_fails(client, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, db = client
    created = item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Item", shop_name="Shop", product_url="https://booth.pm/ja/items/2"),
        primary_upload=_make_upload(tmp_path),
        drive_client=FakeDriveClient(),
    )
    monkeypatch.setattr(booth_info_service, "try_fetch_product_info", lambda url, **kw: None)
    token = _csrf_token(test_client.get(f"/fragments/items/{created.id}/edit").text)

    response = test_client.post(
        f"/fragments/items/{created.id}/refresh-from-booth", headers={"X-CSRF-Token": token}
    )

    assert response.status_code == 200
    assert "BOOTHから情報を取得できませんでした" in response.text
    assert response.headers.get("hx-trigger") is None

    detail = item_service.get_item_detail(db, created.id)
    assert detail.name == "Item"  # unchanged


def test_refresh_from_booth_shows_error_when_no_product_url(client, tmp_path) -> None:
    test_client, db = client
    created = item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Unlinked", shop_name="Shop"),
        primary_upload=_make_upload(tmp_path),
        drive_client=FakeDriveClient(),
    )
    token = _csrf_token(test_client.get(f"/fragments/items/{created.id}/edit").text)

    response = test_client.post(
        f"/fragments/items/{created.id}/refresh-from-booth", headers={"X-CSRF-Token": token}
    )

    assert response.status_code == 200
    assert "BoothURLが設定されていません" in response.text


def test_refresh_from_booth_returns_404_for_missing_item(client) -> None:
    test_client, _db = client
    # No edit panel exists for a nonexistent item to pull a token from --
    # the TOP page's <meta name="csrf-token"> carries the same session token.
    page = test_client.get("/items")
    match = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert match is not None
    token = match.group(1)

    response = test_client.post("/fragments/items/999/refresh-from-booth", headers={"X-CSRF-Token": token})

    assert response.status_code == 404


def test_edit_panel_shows_refresh_button_only_when_linked(client, tmp_path) -> None:
    test_client, db = client
    linked = item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Linked", shop_name="Shop", product_url="https://booth.pm/ja/items/3"),
        primary_upload=_make_upload(tmp_path),
        drive_client=FakeDriveClient(),
    )
    unlinked = item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Unlinked", shop_name="Shop"),
        primary_upload=_make_upload(tmp_path, name="other.zip"),
        drive_client=FakeDriveClient(),
    )

    linked_html = test_client.get(f"/fragments/items/{linked.id}/edit").text
    unlinked_html = test_client.get(f"/fragments/items/{unlinked.id}/edit").text

    assert "BOOTHの最新情報に更新" in linked_html
    assert "BOOTHの最新情報に更新" not in unlinked_html
