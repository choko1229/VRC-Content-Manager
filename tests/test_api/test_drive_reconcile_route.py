from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.drive import folder_layout
from app.drive.fake_drive_client import FakeDriveClient
from app.main import app
from app.services import oauth_service


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


@pytest.fixture()
def client(app_db_session: Session):
    def override_get_db():
        yield app_db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_drive_reconcile_route_reports_not_connected(client: TestClient) -> None:
    page = client.get("/settings")
    csrf_token = _extract_csrf(page.text)

    response = client.post("/fragments/settings/drive-reconcile", headers={"X-CSRF-Token": csrf_token})

    assert response.status_code == 200
    assert "Google Driveが未接続です" in response.text


def test_drive_reconcile_route_imports_new_drive_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_client = FakeDriveClient()
    monkeypatch.setattr(oauth_service, "make_drive_client", lambda db: fake_client)

    root_id = folder_layout.ensure_folder_path(fake_client, folder_layout.ROOT_FOLDER_NAME)
    avatar_id = fake_client.get_or_create_folder("Manuka", root_id)
    item_folder_id = fake_client.get_or_create_folder("SomeShop_RouteTest", avatar_id)
    asset_path = tmp_path / "asset.zip"
    asset_path.write_bytes(b"zip bytes")
    fake_client.upload_file(local_path=asset_path, name="asset.zip", parent_id=item_folder_id)

    page = client.get("/settings")
    csrf_token = _extract_csrf(page.text)

    response = client.post("/fragments/settings/drive-reconcile", headers={"X-CSRF-Token": csrf_token})

    assert response.status_code == 200
    assert "新規インポート 1 件" in response.text
