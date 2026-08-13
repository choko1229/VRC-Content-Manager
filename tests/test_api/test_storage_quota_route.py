"""Coverage for GET /fragments/settings/storage-quota: the settings page's
Drive storage widget (see app/web/fragments/settings.py: storage_quota,
app/templates/partials/storage_quota.html)."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.exceptions import DriveError
from app.db.session import get_db
from app.drive.types import StorageQuota
from app.main import app
from app.services import oauth_service


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


def _meta_csrf_token(test_client: TestClient) -> str:
    page = test_client.get("/settings")
    match = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


class _FakeQuotaClient:
    def __init__(self, quota: StorageQuota) -> None:
        self._quota = quota

    def get_storage_quota(self) -> StorageQuota:
        return self._quota


def test_storage_quota_route_reports_not_connected(client: TestClient) -> None:
    token = _meta_csrf_token(client)

    response = client.get("/fragments/settings/storage-quota", headers={"X-CSRF-Token": token})

    assert response.status_code == 200
    assert "Google Driveが未接続です" in response.text


def test_storage_quota_route_shows_usage_and_percentage(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    quota = StorageQuota(usage_bytes=8 * 1024**3, limit_bytes=16 * 1024**3)  # 8 GiB of 16 GiB, 50%
    monkeypatch.setattr(oauth_service, "make_drive_client", lambda db: _FakeQuotaClient(quota))
    token = _meta_csrf_token(client)

    response = client.get("/fragments/settings/storage-quota", headers={"X-CSRF-Token": token})

    assert response.status_code == 200
    assert "8.0 GB" in response.text
    assert "16.0 GB" in response.text
    assert "50.0%" in response.text


def test_storage_quota_route_shows_unlimited_without_percentage(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    quota = StorageQuota(usage_bytes=1024**3, limit_bytes=None)
    monkeypatch.setattr(oauth_service, "make_drive_client", lambda db: _FakeQuotaClient(quota))
    token = _meta_csrf_token(client)

    response = client.get("/fragments/settings/storage-quota", headers={"X-CSRF-Token": token})

    assert response.status_code == 200
    assert "無制限" in response.text
    assert "%" not in response.text


def test_storage_quota_route_handles_fetch_failure(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomClient:
        def get_storage_quota(self) -> StorageQuota:
            raise DriveError("boom")

    monkeypatch.setattr(oauth_service, "make_drive_client", lambda db: _BoomClient())
    token = _meta_csrf_token(client)

    response = client.get("/fragments/settings/storage-quota", headers={"X-CSRF-Token": token})

    assert response.status_code == 200
    assert "取得できませんでした" in response.text
