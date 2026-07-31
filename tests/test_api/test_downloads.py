from __future__ import annotations

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


def _make_upload(
    tmp_path: Path, name: str = "model.vrm", content: bytes = b"vrm file bytes", extension: str = ".vrm"
) -> ValidatedUpload:
    path = tmp_path / name
    path.write_bytes(content)
    return ValidatedUpload(
        path=path, original_filename=name, size_bytes=len(content), content_type="model/vrm", extension=extension
    )


@pytest.fixture()
def client_with_item(app_db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake_client = FakeDriveClient()
    upload = _make_upload(tmp_path)
    data = ItemCreate(name="DL Test Item", shop_name="Shop")
    created = item_service.create_item_with_file(
        app_db_session, data=data, primary_upload=upload, drive_client=fake_client
    )

    def override_get_db():
        yield app_db_session

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(oauth_service, "make_drive_client", lambda db: fake_client)

    try:
        with TestClient(app) as client:
            yield client, created.id
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_download_item_file_success(client_with_item) -> None:
    client, item_id = client_with_item

    response = client.get(f"/api/v1/items/{item_id}/download")

    assert response.status_code == 200
    assert response.content == b"vrm file bytes"
    assert "model.vrm" in response.headers["content-disposition"]


def test_download_nonexistent_item_returns_404(client_with_item) -> None:
    client, _item_id = client_with_item

    response = client.get("/api/v1/items/999999/download")

    assert response.status_code == 404


def test_download_item_with_japanese_filename(app_db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakeDriveClient()
    upload = _make_upload(tmp_path, name="日本語ファイル名.vrm", content=b"content")
    data = ItemCreate(name="Japanese Filename Item", shop_name="Shop")
    created = item_service.create_item_with_file(
        app_db_session, data=data, primary_upload=upload, drive_client=fake_client
    )

    def override_get_db():
        yield app_db_session

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(oauth_service, "make_drive_client", lambda db: fake_client)

    try:
        with TestClient(app) as client:
            response = client.get(f"/api/v1/items/{created.id}/download")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert response.content == b"content"
