from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.drive import folder_layout
from app.drive.fake_drive_client import FakeDriveClient
from app.main import app
from app.services import app_settings_service, drive_sync_service, oauth_service


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
        drive_sync_service._dirty = False


def test_sync_now_forces_a_real_sync_even_when_not_dirty(
    client: TestClient, app_db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client = FakeDriveClient()
    monkeypatch.setattr(oauth_service, "make_drive_client", lambda db: fake_client)

    root_id = folder_layout.ensure_folder_path(fake_client, folder_layout.ROOT_FOLDER_NAME)
    db_folder_id = fake_client.get_or_create_folder(folder_layout.DB_FOLDER_NAME, root_id)
    uploaded = fake_client.upload_file(
        local_path=drive_sync_service.get_settings().local_db_path,
        name="app.db",
        parent_id=db_folder_id,
        mime_type="application/x-sqlite3",
    )
    app_settings_service.set_setting(app_db_session, "drive_db_file_id", uploaded.id)

    # Simulate the user's exact scenario: they deleted the whole root folder
    # by hand in Drive, then click "今すぐ同期" without having made any
    # further local edits (so the dirty flag is false).
    del fake_client._files[uploaded.id]
    del fake_client._files[db_folder_id]
    del fake_client._files[root_id]
    drive_sync_service._dirty = False

    page = client.get("/settings")
    csrf_token = _extract_csrf(page.text)

    response = client.post("/fragments/settings/sync-now", headers={"X-CSRF-Token": csrf_token})

    assert response.status_code == 200
    assert "同期しました" in response.text
    new_file_id = app_settings_service.get_setting(app_db_session, "drive_db_file_id")
    assert new_file_id != uploaded.id
