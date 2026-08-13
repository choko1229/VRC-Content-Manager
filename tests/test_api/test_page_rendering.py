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


def test_items_new_page_redirects_to_items(page_client: TestClient) -> None:
    # /items/new was folded into the TOP (/items) page's upload button +
    # whole-page drag-and-drop; the route survives only as a redirect for
    # old bookmarks/links.
    response = page_client.get("/items/new", follow_redirects=False)

    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/items"


def test_avatars_page_renders(page_client: TestClient) -> None:
    response = page_client.get("/avatars")

    assert response.status_code == 200


def test_items_list_page_renders_both_views(page_client: TestClient) -> None:
    for view in ("card", "table"):
        response = page_client.get("/items", params={"view": view})
        assert response.status_code == 200


def test_shops_page_renders(page_client: TestClient) -> None:
    response = page_client.get("/shops")

    assert response.status_code == 200
    assert "ショップリスト" in response.text


def _make_upload(tmp_path: Path, name: str) -> ValidatedUpload:
    path = tmp_path / name
    path.write_bytes(b"dummy")
    return ValidatedUpload(
        path=path, original_filename=name, size_bytes=5, content_type="application/octet-stream", extension=".zip"
    )


def test_items_list_shows_grouped_siblings_in_both_views(
    page_client: TestClient, app_db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import thumbnail_service

    monkeypatch.setattr(thumbnail_service, "try_fetch_thumbnail", lambda url, **kw: None)
    item_service.create_item_with_file(
        app_db_session,
        data=ItemCreate(name="Outfit A", shop_name="Shop", product_url="https://booth.pm/ja/items/1"),
        primary_upload=_make_upload(tmp_path, "a.zip"),
        drive_client=FakeDriveClient(),
    )
    item_service.create_item_with_file(
        app_db_session,
        data=ItemCreate(name="Outfit B", shop_name="Shop", product_url="https://booth.pm/ja/items/1"),
        primary_upload=_make_upload(tmp_path, "b.zip"),
        drive_client=FakeDriveClient(),
    )

    for view in ("card", "table"):
        response = page_client.get("/items", params={"view": view})
        assert response.status_code == 200
        assert "Outfit B" in response.text  # the anchor (most recently created)
        assert "Outfit A" in response.text  # the nested sibling


def test_settings_page_includes_shop_management(page_client: TestClient) -> None:
    response = page_client.get("/settings")

    assert response.status_code == 200
    assert "ショップ管理" in response.text


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
    assert 'id="detail-panel"' in response.text  # full shell, not just the fragment


def test_item_detail_htmx_request_returns_fragment_only(
    page_client: TestClient, app_db_session: Session, tmp_path: Path
) -> None:
    item_id = _create_item(app_db_session, tmp_path)

    response = page_client.get(f"/items/{item_id}", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "Render Check Item" in response.text
    assert 'id="detail-panel"' not in response.text  # just the panel's inner content
    assert 'id="main-content"' not in response.text  # not the full page shell


def test_item_edit_page_redirects_to_item_detail(page_client: TestClient, app_db_session: Session, tmp_path: Path) -> None:
    # The dedicated edit page was folded into the detail sidebar's in-place
    # edit mode; the route survives only as a redirect for old bookmarks/links.
    item_id = _create_item(app_db_session, tmp_path)

    response = page_client.get(f"/items/{item_id}/edit", follow_redirects=False)

    assert response.status_code in (302, 307)
    assert response.headers["location"] == f"/items/{item_id}"


def test_item_edit_panel_fragment_renders(page_client: TestClient, app_db_session: Session, tmp_path: Path) -> None:
    item_id = _create_item(app_db_session, tmp_path)

    response = page_client.get(f"/fragments/items/{item_id}/edit")

    assert response.status_code == 200
    assert 'name="csrf_token"' in response.text
