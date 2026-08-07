from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import get_instance_config
from app.core.instance_config import save as save_instance_config
from app.db.session import get_db
from app.main import app
from app.models.booth_library_file import BoothLibraryFile
from app.schemas.item import ItemCreate
from app.services import booth_library_service, item_service
from app.services.upload_service import ValidatedUpload


def _extract_form_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _make_upload(tmp_path: Path, name: str) -> ValidatedUpload:
    path = tmp_path / name
    path.write_bytes(b"dummy content")
    return ValidatedUpload(
        path=path, original_filename=name, size_bytes=13, content_type="application/zip", extension=".zip"
    )


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


def test_update_booth_library_cookie_sets_and_clears(client, migrated_settings) -> None:
    test_client, _db = client
    page = test_client.get("/settings")
    csrf_token = _extract_form_csrf(page.text)

    test_client.post(
        "/settings/booth-library-cookie",
        data={"booth_library_cookie": "session=abc123"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert get_instance_config().booth_library_cookie == "session=abc123"

    settings_page = test_client.get("/settings")
    assert "設定済み" in settings_page.text

    test_client.post(
        "/settings/booth-library-cookie",
        data={"booth_library_cookie": ""},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert get_instance_config().booth_library_cookie == ""


def test_sync_booth_library_fails_cleanly_without_a_cookie(client) -> None:
    test_client, _db = client
    page = test_client.get("/settings")
    csrf_token = _extract_form_csrf(page.text)

    response = test_client.post("/fragments/settings/booth-library/sync", headers={"X-CSRF-Token": csrf_token})

    assert response.status_code == 200
    assert "Cookieが未設定です" in response.text


def test_sync_booth_library_reports_session_expired(
    client, migrated_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, db = client
    config = get_instance_config()
    config.booth_library_cookie = "expired=1"
    save_instance_config(migrated_settings.data_dir, config)

    def fake_sync_library(_db, _cookie, **_kw):
        raise booth_library_service.BoothSessionExpiredError

    monkeypatch.setattr(booth_library_service, "sync_library", fake_sync_library)

    page = test_client.get("/settings")
    csrf_token = _extract_form_csrf(page.text)
    response = test_client.post("/fragments/settings/booth-library/sync", headers={"X-CSRF-Token": csrf_token})

    assert response.status_code == 200
    assert "セッションが無効です" in response.text


def test_sync_booth_library_reports_success_with_count(
    client, migrated_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, db = client
    config = get_instance_config()
    config.booth_library_cookie = "session=abc"
    save_instance_config(migrated_settings.data_dir, config)

    monkeypatch.setattr(booth_library_service, "sync_library", lambda _db, _cookie, **_kw: 7)

    page = test_client.get("/settings")
    csrf_token = _extract_form_csrf(page.text)
    response = test_client.post("/fragments/settings/booth-library/sync", headers={"X-CSRF-Token": csrf_token})

    assert response.status_code == 200
    assert "7件のファイルを同期しました" in response.text


def test_booth_library_match_fragment_returns_nothing_without_a_match(client, tmp_path: Path) -> None:
    test_client, db = client
    created = item_service.create_item_with_file(
        db, data=ItemCreate(name="Unmatched Item", shop_name="未設定"),
        primary_upload=_make_upload(tmp_path, "unmatched.zip"),
    )

    response = test_client.get(f"/fragments/items/{created.id}/booth-library-match")

    assert response.status_code == 200
    assert "js-booth-suggestion" not in response.text


def test_booth_library_match_fragment_returns_a_match(client, tmp_path: Path) -> None:
    test_client, db = client
    created = item_service.create_item_with_file(
        db, data=ItemCreate(name="Matched Item", shop_name="未設定"),
        primary_upload=_make_upload(tmp_path, "known_file.zip"),
    )
    db.add(
        BoothLibraryFile(
            filename="known_file.zip",
            product_url="https://booth.pm/ja/items/999",
            product_name="Known Product",
            shop_name="Known Shop",
            shop_url="https://known.booth.pm/",
            thumbnail_url="https://booth.pximg.net/thumb.jpg",
        )
    )
    db.commit()

    response = test_client.get(f"/fragments/items/{created.id}/booth-library-match")

    assert response.status_code == 200
    assert "js-booth-suggestion" in response.text
    assert "Known Product" in response.text
    assert "Known Shop" in response.text
    assert "ライブラリで完全一致" in response.text


def test_booth_library_match_fragment_returns_nothing_when_already_linked(client, tmp_path: Path) -> None:
    test_client, db = client
    created = item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Already Linked", shop_name="未設定", product_url="https://booth.pm/ja/items/1"),
        primary_upload=_make_upload(tmp_path, "known_file.zip"),
    )
    db.add(
        BoothLibraryFile(
            filename="known_file.zip",
            product_url="https://booth.pm/ja/items/999",
            product_name="Known Product",
            shop_name=None,
            shop_url=None,
            thumbnail_url=None,
        )
    )
    db.commit()

    response = test_client.get(f"/fragments/items/{created.id}/booth-library-match")

    assert response.status_code == 200
    assert "js-booth-suggestion" not in response.text
