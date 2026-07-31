"""Smoke tests: every page-rendering GET route must return 200 without a
template error. These specifically cover routes that no other test exercised
via a plain GET (the ingest form, the edit form) -- important after the
Tailwind/macro-based template rewrite, where a Jinja error (e.g. a filter
that doesn't exist) only surfaces when the template actually renders.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.drive.fake_drive_client import FakeDriveClient
from app.main import app
from app.schemas.item import ItemCreate
from app.services import item_service
from app.services.upload_service import ValidatedUpload


@pytest.fixture()
def page_client(app_db_session: Session):
    def override_get_db():
        yield app_db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_items_new_page_renders(page_client: TestClient) -> None:
    response = page_client.get("/items/new")

    assert response.status_code == 200
    assert 'name="csrf_token"' in response.text


def test_items_list_page_renders_both_views(page_client: TestClient) -> None:
    for view in ("card", "table"):
        response = page_client.get("/items", params={"view": view})
        assert response.status_code == 200


def test_shops_page_renders(page_client: TestClient) -> None:
    response = page_client.get("/shops")

    assert response.status_code == 200


def _create_item(db: Session, tmp_path: Path) -> int:
    fake_client = FakeDriveClient()
    upload_path = tmp_path / "item.vrm"
    upload_path.write_bytes(b"content")
    upload = ValidatedUpload(
        path=upload_path, original_filename="item.vrm", size_bytes=7, content_type="model/vrm", extension=".vrm"
    )
    created = item_service.create_item_with_file(
        db, data=ItemCreate(name="Render Check Item", shop_name="Shop"), primary_upload=upload, drive_client=fake_client
    )
    return created.id


def test_item_detail_page_renders(page_client: TestClient, app_db_session: Session, tmp_path: Path) -> None:
    item_id = _create_item(app_db_session, tmp_path)

    response = page_client.get(f"/items/{item_id}")

    assert response.status_code == 200
    assert "Render Check Item" in response.text


def test_item_edit_page_renders(page_client: TestClient, app_db_session: Session, tmp_path: Path) -> None:
    item_id = _create_item(app_db_session, tmp_path)

    response = page_client.get(f"/items/{item_id}/edit")

    assert response.status_code == 200
    assert 'name="csrf_token"' in response.text
