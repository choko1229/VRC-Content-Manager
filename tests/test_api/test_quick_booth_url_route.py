"""Coverage for POST /fragments/items/{id}/quick-booth-url: the TOP page's
post-upload panel (see app/web/fragments/items.py: quick_booth_url_fragment,
app/templates/items/list.html's quick-booth-panel)."""

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


def _csrf_token(test_client: TestClient) -> str:
    page = test_client.get("/items")
    match = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


def _make_upload(tmp_path, name: str = "asset.zip", content: bytes = b"dummy") -> ValidatedUpload:
    path = tmp_path / name
    path.write_bytes(content)
    return ValidatedUpload(
        path=path, original_filename=name, size_bytes=len(content), content_type="application/zip", extension=".zip"
    )


def test_quick_booth_url_links_item_and_shows_fetched_name(client, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, db = client
    created = item_service.create_item_with_file(
        db, data=ItemCreate(name="asset", shop_name="未設定"), primary_upload=_make_upload(tmp_path), drive_client=FakeDriveClient()
    )
    info = booth_info_service.BoothProductInfo(
        name="Fetched Name", shop_name="Fetched Shop", shop_url=None, price=None, image_url=None, description=None
    )
    monkeypatch.setattr(booth_info_service, "try_fetch_product_info", lambda url, **kw: info)
    token = _csrf_token(test_client)

    response = test_client.post(
        f"/fragments/items/{created.id}/quick-booth-url",
        headers={"X-CSRF-Token": token},
        data={"product_url": "https://fetched.booth.pm/items/1"},
    )

    assert response.status_code == 200
    assert f'id="quick-booth-row-{created.id}"' in response.text
    assert "Fetched Name" in response.text
    assert "Fetched Shop" in response.text
    assert response.headers.get("hx-trigger") == "item-saved"

    detail = item_service.get_item_detail(db, created.id)
    assert detail.product_url == "https://fetched.booth.pm/items/1"
    assert detail.name == "Fetched Name"


def test_quick_booth_url_saves_url_and_warns_when_fetch_fails(client, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, db = client
    created = item_service.create_item_with_file(
        db, data=ItemCreate(name="asset", shop_name="未設定"), primary_upload=_make_upload(tmp_path), drive_client=FakeDriveClient()
    )
    monkeypatch.setattr(booth_info_service, "try_fetch_product_info", lambda url, **kw: None)
    token = _csrf_token(test_client)

    response = test_client.post(
        f"/fragments/items/{created.id}/quick-booth-url",
        headers={"X-CSRF-Token": token},
        data={"product_url": "https://unreachable.booth.pm/items/9"},
    )

    assert response.status_code == 200
    assert "BOOTHから情報を取得できませんでした" in response.text

    detail = item_service.get_item_detail(db, created.id)
    assert detail.product_url == "https://unreachable.booth.pm/items/9"
    assert detail.name == "asset"  # unchanged


def test_quick_booth_url_rejects_blank_url(client, tmp_path) -> None:
    test_client, db = client
    created = item_service.create_item_with_file(
        db, data=ItemCreate(name="asset", shop_name="未設定"), primary_upload=_make_upload(tmp_path), drive_client=FakeDriveClient()
    )
    token = _csrf_token(test_client)

    response = test_client.post(
        f"/fragments/items/{created.id}/quick-booth-url", headers={"X-CSRF-Token": token}, data={"product_url": "  "}
    )

    assert response.status_code == 200
    assert "BoothURLを入力してください" in response.text
    detail = item_service.get_item_detail(db, created.id)
    assert detail.product_url is None


def test_quick_booth_url_returns_404_for_missing_item(client) -> None:
    test_client, _db = client
    token = _csrf_token(test_client)

    response = test_client.post(
        "/fragments/items/999/quick-booth-url", headers={"X-CSRF-Token": token}, data={"product_url": "https://booth.pm/x"}
    )

    assert response.status_code == 404
