from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_db, get_sessionmaker
from app.drive.fake_drive_client import FakeDriveClient
from app.main import app
from app.models.item_file import FileRole, ItemFile
from app.schemas.item import ItemCreate
from app.services import item_service, local_cache_service, oauth_service
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


def test_download_item_file_nests_filename_under_app_subfolder(client_with_item) -> None:
    # A "/" in a FileResponse filename isn't cosmetic -- Chromium browsers
    # create the corresponding subfolder under the default Downloads
    # directory rather than sanitizing it away, so this is what actually
    # routes downloads into Downloads/VRC Content Manager/ -- see
    # app/api/routers/downloads.py's _DOWNLOAD_SUBFOLDER.
    client, item_id = client_with_item

    response = client.get(f"/api/v1/items/{item_id}/download")

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "VRC" in disposition and "Content" in disposition and "Manager" in disposition
    assert "model.vrm" in disposition
    # The literal "/" between the subfolder and filename must survive
    # whichever Content-Disposition form (plain filename= vs percent-encoded
    # filename*=utf-8'') gets used -- it's what Chromium keys off of.
    assert "%2F" not in disposition.upper()


def test_download_item_file_head_warms_cache_with_no_body(client_with_item) -> None:
    # See base.html's download-feedback script: it HEADs this same URL first
    # (to show a toast through the wait and surface a real error) before
    # triggering the actual native download -- so HEAD must run the same
    # resolve_local_path/cache-warming work GET does, just without a body.
    client, item_id = client_with_item

    response = client.head(f"/api/v1/items/{item_id}/download")

    assert response.status_code == 200
    assert response.content == b""
    assert "model.vrm" in response.headers["content-disposition"]
    assert response.headers.get("content-length") == str(len(b"vrm file bytes"))

    # And the cache it warmed is what the follow-up GET actually serves.
    get_response = client.get(f"/api/v1/items/{item_id}/download")
    assert get_response.status_code == 200
    assert get_response.content == b"vrm file bytes"


def test_download_nonexistent_item_head_returns_404_with_no_body(client_with_item) -> None:
    client, _item_id = client_with_item

    response = client.head("/api/v1/items/999999/download")

    assert response.status_code == 404
    assert response.content == b""


def test_download_attachment_file_head_and_get(client_with_item) -> None:
    # download_item_attachment_file is a separate route from the primary-
    # file one above (the @router.head stacking has to be repeated on it
    # too) -- covers e.g. a duplicate-BoothURL upload folded in as an
    # ATTACHMENT via item_service.merge_item_into.
    client, item_id = client_with_item
    session_local = get_sessionmaker()
    db = session_local()
    try:
        local_cache_service.pending_upload_path("attachment-stored.zip").write_bytes(b"attachment bytes")
        attachment = ItemFile(
            item_id=item_id,
            file_role=FileRole.ATTACHMENT,
            original_filename="attachment.zip",
            stored_filename="attachment-stored.zip",
            content_type="application/zip",
            size_bytes=len(b"attachment bytes"),
        )
        db.add(attachment)
        db.commit()
        file_id = attachment.id
    finally:
        db.close()

    head_response = client.head(f"/api/v1/items/{item_id}/files/{file_id}/download")
    assert head_response.status_code == 200
    assert head_response.content == b""
    assert "attachment.zip" in head_response.headers["content-disposition"]

    get_response = client.get(f"/api/v1/items/{item_id}/files/{file_id}/download")
    assert get_response.status_code == 200
    assert get_response.content == b"attachment bytes"


def test_download_item_file_populates_and_reuses_download_cache(client_with_item) -> None:
    client, item_id = client_with_item

    # The cache directory should gain exactly one entry after the first
    # download, and that same entry should be reused (not re-fetched) on a
    # second request.
    cache_dir = get_settings().download_cache_dir
    before = set(cache_dir.iterdir()) if cache_dir.exists() else set()

    first = client.get(f"/api/v1/items/{item_id}/download")
    after_first = set(cache_dir.iterdir())
    assert first.status_code == 200
    new_entries = after_first - before
    assert len(new_entries) == 1
    cached_file = new_entries.pop()
    mtime_after_first = cached_file.stat().st_mtime

    second = client.get(f"/api/v1/items/{item_id}/download")

    assert second.status_code == 200
    assert second.content == first.content
    assert set(cache_dir.iterdir()) == after_first  # no new cache entry created
    assert cached_file.stat().st_mtime >= mtime_after_first  # touched (sliding TTL), not re-downloaded as a new file


def test_download_pending_item_serves_from_local_cache_without_drive(
    app_db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload = _make_upload(tmp_path, content=b"still pending bytes")
    created = item_service.create_item_with_file(
        app_db_session, data=ItemCreate(name="Pending DL", shop_name="Shop"), primary_upload=upload
    )

    def _fail_if_called(db):
        raise AssertionError("should not need a Drive client for a pending (not-yet-synced) file")

    def override_get_db():
        yield app_db_session

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(oauth_service, "make_drive_client", _fail_if_called)

    try:
        with TestClient(app) as client:
            response = client.get(f"/api/v1/items/{created.id}/download")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert response.content == b"still pending bytes"


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
