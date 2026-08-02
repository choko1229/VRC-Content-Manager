"""End-to-end HTTP coverage for the chunked upload path (app/web/pages/items.py:
create_item_chunked_init/part/complete), used by the frontend for any file
larger than chunked_upload_service.CHUNK_SIZE_MB so a single request never
has to carry more than a proxy/CDN's body-size cap allows."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.drive.fake_drive_client import FakeDriveClient
from app.main import app
from app.services import item_service, oauth_service, upload_sync_service


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


def _meta_csrf_token(page_html: str) -> str:
    match = re.search(r'name="csrf-token" content="([^"]+)"', page_html)
    assert match is not None
    return match.group(1)


def _upload_in_chunks(test_client: TestClient, token: str, filename: str, content: bytes, chunk_size: int) -> dict:
    init_resp = test_client.post(
        "/items/new/chunked/init",
        headers={"X-CSRF-Token": token},
        json={"filename": filename, "size": len(content)},
    )
    assert init_resp.status_code == 200, init_resp.text
    upload_id = init_resp.json()["upload_id"]

    chunks = [content[i : i + chunk_size] for i in range(0, len(content), chunk_size)]
    for index, chunk in enumerate(chunks):
        part_resp = test_client.post(
            f"/items/new/chunked/{upload_id}/{index}",
            headers={"X-CSRF-Token": token, "Content-Type": "application/octet-stream"},
            content=chunk,
        )
        assert part_resp.status_code == 200, part_resp.text

    complete_resp = test_client.post(
        f"/items/new/chunked/{upload_id}/complete",
        headers={"X-CSRF-Token": token},
    )
    return complete_resp


def test_chunked_upload_full_flow_creates_item(client) -> None:
    test_client, db = client
    token = _meta_csrf_token(test_client.get("/items").text)
    content = b"PK\x03\x04" + (b"x" * 40)

    complete_resp = _upload_in_chunks(test_client, token, "Big Avatar.zip", content, chunk_size=12)

    assert complete_resp.status_code == 200, complete_resp.text
    item_id = complete_resp.json()["item_id"]
    detail = item_service.get_item_detail(db, item_id)
    assert detail.name == "Big Avatar"
    assert detail.shop_name == "未設定"


def test_chunked_upload_reassembled_content_matches_original(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The complete route fires a background sync (asyncio.create_task) after
    # returning -- on a real fake_client that would race the download request
    # below (a different session might observe synced_at flip mid-request,
    # non-deterministically). Disabling it here keeps this test about
    # reassembly correctness, not that unrelated timing.
    monkeypatch.setattr(upload_sync_service, "sync_pending_now", lambda: 0)
    test_client, db = client
    token = _meta_csrf_token(test_client.get("/items").text)
    content = b"PK\x03\x04" + (b"abcdefghij" * 5)

    complete_resp = _upload_in_chunks(test_client, token, "reassemble.zip", content, chunk_size=7)
    item_id = complete_resp.json()["item_id"]

    download = test_client.get(f"/api/v1/items/{item_id}/download")
    assert download.status_code == 200
    assert download.content == content


def test_chunked_upload_init_rejects_disallowed_extension(client) -> None:
    test_client, _db = client
    token = _meta_csrf_token(test_client.get("/items").text)

    response = test_client.post(
        "/items/new/chunked/init",
        headers={"X-CSRF-Token": token},
        json={"filename": "virus.exe", "size": 10},
    )

    assert response.status_code == 422
    assert "許可されていないファイル形式" in response.json()["detail"]


def test_chunked_upload_init_rejects_oversized_declared_total(client) -> None:
    test_client, _db = client
    token = _meta_csrf_token(test_client.get("/items").text)

    response = test_client.post(
        "/items/new/chunked/init",
        headers={"X-CSRF-Token": token},
        json={"filename": "asset.zip", "size": 600 * 1024 * 1024},
    )

    assert response.status_code == 422
    assert "上限" in response.json()["detail"]


def test_chunked_upload_part_for_unknown_upload_id_returns_422(client) -> None:
    test_client, _db = client
    token = _meta_csrf_token(test_client.get("/items").text)

    response = test_client.post(
        "/items/new/chunked/does-not-exist/0",
        headers={"X-CSRF-Token": token, "Content-Type": "application/octet-stream"},
        content=b"data",
    )

    assert response.status_code == 422


def test_chunked_upload_complete_before_all_chunks_sent_returns_422(client) -> None:
    test_client, _db = client
    token = _meta_csrf_token(test_client.get("/items").text)

    init_resp = test_client.post(
        "/items/new/chunked/init",
        headers={"X-CSRF-Token": token},
        json={"filename": "incomplete.zip", "size": 100},
    )
    upload_id = init_resp.json()["upload_id"]
    test_client.post(
        f"/items/new/chunked/{upload_id}/0",
        headers={"X-CSRF-Token": token, "Content-Type": "application/octet-stream"},
        content=b"only 20 bytes here..",
    )

    response = test_client.post(f"/items/new/chunked/{upload_id}/complete", headers={"X-CSRF-Token": token})

    assert response.status_code == 422
    assert "不完全" in response.json()["detail"]


def test_chunked_upload_init_requires_csrf(client) -> None:
    test_client, _db = client

    response = test_client.post("/items/new/chunked/init", json={"filename": "asset.zip", "size": 10})

    assert response.status_code == 403
