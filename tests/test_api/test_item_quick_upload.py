"""API-level coverage for the Google Drive-style flow: drop a file, get a
minimally-populated item immediately, fill in details afterward via the
sidebar edit panel (see app/web/pages/items.py: create_item and
app/web/fragments/items.py: submit_edit_item_panel_fragment)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.drive.fake_drive_client import FakeDriveClient
from app.main import app
from app.schemas.item import ItemCreate, ItemUpdate
from app.services import avatar_service, item_service, oauth_service
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


def _meta_csrf_token(page_html: str) -> str:
    # The TOP page's upload button submits via fetch(), reading the token
    # from base.html's <meta name="csrf-token"> rather than a form field.
    match = re.search(r'name="csrf-token" content="([^"]+)"', page_html)
    assert match is not None
    return match.group(1)


def test_quick_upload_creates_item_with_derived_name_and_redirects_to_detail(client) -> None:
    test_client, db = client
    token = _meta_csrf_token(test_client.get("/items").text)

    response = test_client.post(
        "/items/new",
        headers={"X-CSRF-Token": token},
        files={"file": ("Cool Avatar v2.unitypackage", b"dummy content", "application/octet-stream")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    match = re.match(r"^/items/(\d+)$", response.headers["location"])
    assert match is not None
    item_id = int(match.group(1))

    detail = item_service.get_item_detail(db, item_id)
    assert detail.name == "Cool Avatar v2"
    assert detail.shop_name == "未設定"
    assert detail.has_thumbnail is False


def test_quick_upload_without_file_returns_error(client) -> None:
    test_client, _db = client
    token = _meta_csrf_token(test_client.get("/items").text)

    response = test_client.post("/items/new", headers={"X-CSRF-Token": token})

    assert response.status_code == 200  # followed the redirect back to /items
    assert "ファイルを選択してください" in response.text


def test_quick_upload_rejects_disallowed_extension(client) -> None:
    test_client, _db = client
    token = _meta_csrf_token(test_client.get("/items").text)

    response = test_client.post(
        "/items/new",
        headers={"X-CSRF-Token": token},
        files={"file": ("virus.exe", b"dummy content", "application/octet-stream")},
    )

    assert response.status_code == 200
    assert "許可されていないファイル形式です" in response.text


def test_edit_item_with_thumbnail_upload_attaches_thumbnail(client, tmp_path: Path) -> None:
    test_client, db = client
    fake_client = FakeDriveClient()

    created = item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Needs Details", shop_name="未設定"),
        primary_upload=_make_upload(tmp_path, "item2.zip"),
        drive_client=fake_client,
    )

    token = _csrf_token(test_client.get(f"/fragments/items/{created.id}/edit").text)

    response = test_client.post(
        f"/fragments/items/{created.id}/edit",
        headers={"X-CSRF-Token": token},
        data={
            "csrf_token": token,
            "name": "Real Name",
            "shop_name": "Real Shop",
        },
        files={"thumbnail": ("thumb.png", b"\x89PNG\r\n", "image/png")},
    )

    assert response.status_code == 200
    detail = item_service.get_item_detail(db, created.id)
    assert detail.name == "Real Name"
    assert detail.has_thumbnail is True

    edit_panel = test_client.get(f"/fragments/items/{created.id}/edit")
    assert edit_panel.status_code == 200
    assert f'/items/{created.id}/thumbnail' in edit_panel.text


def test_edit_panel_preserves_fields_not_in_the_sidebar_form(client, tmp_path: Path) -> None:
    """The sidebar edit form only exposes 8 fields; price/dates/status/
    favorite/license aren't on it and must survive a save untouched (still
    visible read-only in the detail view, still editable via bulk-edit)."""
    test_client, db = client
    fake_client = FakeDriveClient()

    created = item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Preserve Me", shop_name="未設定", price=1500, is_favorite=True),
        primary_upload=_make_upload(tmp_path, "preserve.zip"),
        drive_client=fake_client,
    )

    item_service.update_item(
        db,
        created.id,
        ItemUpdate(
            name="Preserve Me",
            shop_name="未設定",
            price=1500,
            is_favorite=True,
            status_code="in_use",
            commercial_use="yes",
            license_note="original note",
        ),
    )

    token = _csrf_token(test_client.get(f"/fragments/items/{created.id}/edit").text)
    response = test_client.post(
        f"/fragments/items/{created.id}/edit",
        headers={"X-CSRF-Token": token},
        data={"csrf_token": token, "name": "Preserve Me", "shop_name": "未設定", "description": "new description"},
    )

    assert response.status_code == 200
    detail = item_service.get_item_detail(db, created.id)
    assert detail.description == "new description"
    assert detail.price == 1500
    assert detail.is_favorite is True
    assert detail.status_code == "in_use"
    assert detail.commercial_use.value == "yes"
    assert detail.license_note == "original note"


def _register_avatar(db, tmp_path: Path, name: str):
    fake_client = FakeDriveClient()
    base_item = item_service.create_item_with_file(
        db,
        data=ItemCreate(name=f"{name} base model", shop_name="未設定"),
        primary_upload=_make_upload(tmp_path, f"{name}-base.zip"),
        drive_client=fake_client,
    )
    return avatar_service.set_item_as_avatar(db, base_item.id, name=name, memo=None)


def test_edit_panel_selects_avatars_by_checking_registered_ones(client, tmp_path: Path) -> None:
    test_client, db = client
    manuka = _register_avatar(db, tmp_path, "Manuka")
    rusk = _register_avatar(db, tmp_path, "Rusk")

    created = item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Avatar Merge", shop_name="未設定", avatars=["Manuka"]),
        primary_upload=_make_upload(tmp_path, "avatars.zip"),
        drive_client=FakeDriveClient(),
    )

    token = _csrf_token(test_client.get(f"/fragments/items/{created.id}/edit").text)
    response = test_client.post(
        f"/fragments/items/{created.id}/edit",
        headers={"X-CSRF-Token": token},
        data={
            "csrf_token": token,
            "name": "Avatar Merge",
            "shop_name": "未設定",
            "avatars": [str(manuka.id), str(rusk.id)],
        },
    )

    assert response.status_code == 200
    detail = item_service.get_item_detail(db, created.id)
    assert sorted(detail.avatars) == ["Manuka", "Rusk"]


def test_edit_panel_can_register_item_as_avatar(client, tmp_path: Path) -> None:
    test_client, db = client
    created = item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Future Avatar", shop_name="未設定"),
        primary_upload=_make_upload(tmp_path, "future-avatar.zip"),
        drive_client=FakeDriveClient(),
    )

    token = _csrf_token(test_client.get(f"/fragments/items/{created.id}/edit").text)
    response = test_client.post(
        f"/fragments/items/{created.id}/edit",
        headers={"X-CSRF-Token": token},
        data={
            "csrf_token": token,
            "name": "Future Avatar",
            "shop_name": "未設定",
            "is_avatar": "true",
            "avatar_name": "マヌカ",
            "avatar_memo": "改変可",
        },
    )

    assert response.status_code == 200
    assert "アバター" in response.text  # the registered-avatar badge shows in the returned detail panel

    avatar = avatar_service.get_avatar_for_item(db, created.id)
    assert avatar is not None
    assert avatar.name == "マヌカ"
    assert avatar.memo == "改変可"

    # and it now shows up as a selectable option for other items
    options = avatar_service.list_avatar_options(db)
    assert [o.name for o in options] == ["マヌカ"]


def test_edit_panel_unregisters_avatar_when_toggle_unchecked(client, tmp_path: Path) -> None:
    test_client, db = client
    created = item_service.create_item_with_file(
        db,
        data=ItemCreate(name="No Longer Avatar", shop_name="未設定"),
        primary_upload=_make_upload(tmp_path, "unregister.zip"),
        drive_client=FakeDriveClient(),
    )
    avatar_service.set_item_as_avatar(db, created.id, name="一時アバター", memo=None)

    token = _csrf_token(test_client.get(f"/fragments/items/{created.id}/edit").text)
    response = test_client.post(
        f"/fragments/items/{created.id}/edit",
        headers={"X-CSRF-Token": token},
        data={"csrf_token": token, "name": "No Longer Avatar", "shop_name": "未設定"},  # is_avatar omitted = unchecked
    )

    assert response.status_code == 200
    assert avatar_service.get_avatar_for_item(db, created.id) is None


def test_edit_panel_rejects_duplicate_avatar_name(client, tmp_path: Path) -> None:
    test_client, db = client
    _register_avatar(db, tmp_path, "既存アバター")
    created = item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Duplicate Attempt", shop_name="未設定"),
        primary_upload=_make_upload(tmp_path, "dup.zip"),
        drive_client=FakeDriveClient(),
    )

    token = _csrf_token(test_client.get(f"/fragments/items/{created.id}/edit").text)
    response = test_client.post(
        f"/fragments/items/{created.id}/edit",
        headers={"X-CSRF-Token": token},
        data={
            "csrf_token": token,
            "name": "Duplicate Attempt",
            "shop_name": "未設定",
            "is_avatar": "true",
            "avatar_name": "既存アバター",
        },
    )

    assert response.status_code == 422
    assert "既に使用されています" in response.text
    assert avatar_service.get_avatar_for_item(db, created.id) is None


def test_edit_panel_saved_description_shows_in_detail_view(client, tmp_path: Path) -> None:
    test_client, db = client
    fake_client = FakeDriveClient()

    created = item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Description Check", shop_name="未設定"),
        primary_upload=_make_upload(tmp_path, "desc.zip"),
        drive_client=fake_client,
    )

    token = _csrf_token(test_client.get(f"/fragments/items/{created.id}/edit").text)
    response = test_client.post(
        f"/fragments/items/{created.id}/edit",
        headers={"X-CSRF-Token": token},
        data={
            "csrf_token": token,
            "name": "Description Check",
            "shop_name": "未設定",
            "description": "説明文の表示確認",
        },
    )

    assert response.status_code == 200
    assert "説明文の表示確認" in response.text  # the returned detail panel shows it immediately

    detail_page = test_client.get(f"/items/{created.id}")
    assert "説明文の表示確認" in detail_page.text  # and it survives a fresh page load too


def test_delete_item_via_detail_panel(client, tmp_path: Path) -> None:
    test_client, db = client
    fake_client = FakeDriveClient()

    created = item_service.create_item_with_file(
        db,
        data=ItemCreate(name="Delete Me", shop_name="未設定"),
        primary_upload=_make_upload(tmp_path, "item3.zip"),
        drive_client=fake_client,
    )
    token = _meta_csrf_token(test_client.get("/items").text)

    response = test_client.delete(f"/fragments/items/{created.id}", headers={"X-CSRF-Token": token})

    assert response.status_code == 200
    assert "商品を選択すると詳細が表示されます" in response.text

    with pytest.raises(Exception):
        item_service.get_item_detail(db, created.id)


def test_bulk_update_route_applies_to_multiple_selected_items(client, tmp_path: Path) -> None:
    test_client, db = client
    fake_client = FakeDriveClient()

    a = item_service.create_item_with_file(
        db, data=ItemCreate(name="Bulk Route A", shop_name="Shop"),
        primary_upload=_make_upload(tmp_path, "bulk-a.zip"), drive_client=fake_client,
    )
    b = item_service.create_item_with_file(
        db, data=ItemCreate(name="Bulk Route B", shop_name="Shop"),
        primary_upload=_make_upload(tmp_path, "bulk-b.zip"), drive_client=fake_client,
    )
    token = _meta_csrf_token(test_client.get("/items").text)

    response = test_client.post(
        "/fragments/items/bulk-update",
        headers={"X-CSRF-Token": token},
        data={
            "item_ids": f"{a.id},{b.id}",
            "status_code": "in_use",
            "add_tags": "一括タグ",
            "add_avatars": "",
            "favorite": "true",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"updated": 2}

    for item_id in (a.id, b.id):
        detail = item_service.get_item_detail(db, item_id)
        assert detail.status_code == "in_use"
        assert detail.tags == ["一括タグ"]
        assert detail.is_favorite is True


def test_bulk_update_route_requires_csrf(client) -> None:
    test_client, _db = client

    response = test_client.post("/fragments/items/bulk-update", data={"item_ids": "1,2"})

    assert response.status_code == 403
