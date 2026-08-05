from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.db.session import get_db
from app.main import app
from app.schemas.item import ItemCreate
from app.services import item_service
from app.services.upload_service import ValidatedUpload


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _make_upload(tmp_path: Path, name: str, *, unique_suffix: str) -> ValidatedUpload:
    # The on-disk source filename must be unique per call (it becomes the
    # local pending-upload cache filename) even when two uploads share the
    # same *original* filename -- which is exactly the scenario these tests
    # need to set up.
    path = tmp_path / f"{unique_suffix}-{name}"
    path.write_bytes(b"dummy content")
    return ValidatedUpload(
        path=path, original_filename=name, size_bytes=13, content_type="application/zip", extension=".zip"
    )


def _create_item(db: Session, tmp_path: Path, *, item_name: str, filename: str) -> int:
    created = item_service.create_item_with_file(
        db,
        data=ItemCreate(name=item_name, shop_name="Shop"),
        primary_upload=_make_upload(tmp_path, filename, unique_suffix=item_name),
    )
    return created.id


@pytest.fixture()
def client(app_db_session: Session):
    def override_get_db():
        yield app_db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client, app_db_session
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_duplicate_files_scan_reports_no_duplicates(client, tmp_path: Path) -> None:
    test_client, db = client
    _create_item(db, tmp_path, item_name="A", filename="a.zip")
    page = test_client.get("/settings")
    csrf_token = _extract_csrf(page.text)

    response = test_client.post("/fragments/settings/duplicate-files/scan", headers={"X-CSRF-Token": csrf_token})

    assert response.status_code == 200
    assert "見つかりませんでした" in response.text


def test_duplicate_files_scan_and_merge_flow(client, tmp_path: Path) -> None:
    test_client, db = client
    ids = [
        _create_item(db, tmp_path, item_name=name, filename="same.zip") for name in ("Keep This", "Fold Me")
    ]

    page = test_client.get("/settings")
    csrf_token = _extract_csrf(page.text)

    scan = test_client.post("/fragments/settings/duplicate-files/scan", headers={"X-CSRF-Token": csrf_token})
    assert scan.status_code == 200
    assert "1 組見つかりました" in scan.text
    assert "Keep This" in scan.text
    assert "Fold Me" in scan.text

    merge = test_client.post(
        "/fragments/settings/duplicate-files/merge",
        headers={"X-CSRF-Token": csrf_token},
        data={"item_ids": [str(i) for i in ids]},
    )

    assert merge.status_code == 200
    assert "統合しました" in merge.text
    assert item_service.get_item_detail(db, ids[0]).id == ids[0]
    with pytest.raises(NotFoundError):
        item_service.get_item_detail(db, ids[1])
