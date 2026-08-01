"""Coverage for /fragments/shops/{id}/fetch-info: BOOTH shop icon/description
auto-fetch (see app/web/fragments/shops.py: fetch_shop_info_fragment)."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.main import app
from app.schemas.shop import ShopCreate
from app.services import booth_info_service, shop_service


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


def _csrf_token(test_client: TestClient) -> str:
    page = test_client.get("/shops")
    match = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


def test_fetch_shop_info_updates_icon_and_memo(client, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, db = client
    shop = shop_service.create_shop(db, ShopCreate(name="Fetchable", url="https://fetchable.booth.pm/"))
    fetched = booth_info_service.BoothShopInfo(
        name="Fetchable", icon_url="https://example.com/icon.png", description="お店の説明"
    )
    monkeypatch.setattr(booth_info_service, "try_fetch_shop_info", lambda url: fetched)
    token = _csrf_token(test_client)

    response = test_client.post(
        f"/fragments/shops/{shop.id}/fetch-info", headers={"X-CSRF-Token": token}
    )

    assert response.status_code == 200
    assert "example.com/icon.png" in response.text
    updated = next(s for s in shop_service.list_shops(db) if s.id == shop.id)
    assert updated.icon_url == "https://example.com/icon.png"
    assert updated.memo == "お店の説明"


def test_fetch_shop_info_without_url_shows_error(client) -> None:
    test_client, db = client
    shop = shop_service.create_shop(db, ShopCreate(name="No URL"))
    token = _csrf_token(test_client)

    response = test_client.post(
        f"/fragments/shops/{shop.id}/fetch-info", headers={"X-CSRF-Token": token}
    )

    assert response.status_code == 200
    assert "ショップURLが未設定です" in response.text


def test_fetch_shop_info_handles_fetch_failure(client, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, db = client
    shop = shop_service.create_shop(db, ShopCreate(name="Unreachable", url="https://unreachable.booth.pm/"))
    monkeypatch.setattr(booth_info_service, "try_fetch_shop_info", lambda url: None)
    token = _csrf_token(test_client)

    response = test_client.post(
        f"/fragments/shops/{shop.id}/fetch-info", headers={"X-CSRF-Token": token}
    )

    assert response.status_code == 200
    assert "自動取得できませんでした" in response.text
