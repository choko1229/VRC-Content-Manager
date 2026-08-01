"""API-level coverage for the Google Drive-style flow: drop a file, get a
minimally-populated item immediately, fill in details afterward on the edit
page (see app/web/pages/items.py: create_item / submit_edit_item)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.drive.fake_drive_client import FakeDriveClient
from app.main import app
from app.schemas.item import ItemCreate
from app.services import item_service, oauth_service
from app.services.upload_service import ValidatedUpload


def _make_upload(tmp_path: Path, name: str, content: bytes = b"dummy") -> ValidatedUpload:
    path = tmp_path / name
    path.write_bytes(content)
    return ValidatedUpload(
        path=path, original_filename=name, size_bytes=len(content), content_type="application/zip", extension=".zip"
    )


@pytest.fixture()
def client(app_db_session: Session, monkeypatch: pytest.MonkeyPatch):
    fake_client = FakeDriveClient()
    monkeypatch.setattr(oauth_service, "make_drive_client", lambda db: fake_client)

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


def test_quick_upload_creates_item_with_derived_name_and_redirects_to_edit(client) -> None:
    test_client, db = client
    token = _csrf_token(test_client.get("/items/new").text)

    response = test_client.post(
        "/items/new",
        data={"csrf_token": token},
        files={"file": ("Cool Avatar v2.unitypackage", b"dummy content", "application/octet-stream")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    match = re.match(r"^/items/(\d+)/edit\?uploaded=1$", response.headers["location"])
    assert match is not None
    item_id = int(match.group(1))

    detail = item_service.get_item_detail(db, item_id)
    assert detail.name == "Cool Avatar v2"
    assert detail.shop_name == "未設定"
    assert detail.has_thumbnail is False


def test_quick_upload_without_file_returns_error(client) -> None:
    test_client, _db = client
    token = _csrf_token(test_client.get("/items/new").text)

    response = test_client.post("/items/new", data={"csrf_token": token})

    assert response.status_code == 422
    assert "ファイルを選択してください" in response.text


def test_quick_upload_rejects_disallowed_extension(client) -> None:
    test_client, _db = client
    token = _csrf_token(test_client.get("/items/new").text)

    response = test_client.post(
        "/items/new",
        data={"csrf_token": token},
        files={"file": ("virus.exe", b"dummy content", "application/octet-stream")},
    )

    assert response.status_code == 422
    assert "許可されていないファイル形式です" in response.text


def test_edit_page_shows_upload_success_banner(client, tmp_path: Path) -> None:
    test_client, db = client
    fake_client = FakeDriveClient()

    created = item_service.create_item_with_file(
        db,
        data=ItemCreate(name="未設定 Item", shop_name="未設定"),
        primary_upload=_make_upload(tmp_path, "item.zip"),
        drive_client=fake_client,
    )

    response = test_client.get(f"/items/{created.id}/edit", params={"uploaded": "1"})

    assert response.status_code == 200
    assert "アップロードしました" in response.text


def test_edit_item_with_thumbnail_upload_attaches_thumbnail(client, tmp_path: Path) -> None:
    test_client, db = client
    fake_client = FakeDriveClient()

    created = item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Needs Details", shop_name="未設定"),
        primary_upload=_make_upload(tmp_path, "item2.zip"),
        drive_client=fake_client,
    )

    token = _csrf_token(test_client.get(f"/items/{created.id}/edit").text)

    response = test_client.post(
        f"/items/{created.id}/edit",
        data={
            "csrf_token": token,
            "name": "Real Name",
            "shop_name": "Real Shop",
        },
        files={"thumbnail": ("thumb.png", b"\x89PNG\r\n", "image/png")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    detail = item_service.get_item_detail(db, created.id)
    assert detail.name == "Real Name"
    assert detail.has_thumbnail is True

    edit_page = test_client.get(f"/items/{created.id}/edit")
    assert edit_page.status_code == 200
    assert "現在のサムネイル" in edit_page.text
