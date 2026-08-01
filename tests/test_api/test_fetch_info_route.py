"""Coverage for /fragments/items/fetch-info: BOOTH metadata prefill for the
sidebar edit panel (see app/web/fragments/items.py: fetch_info_fragment)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.main import app
from app.models.item import Item
from app.services import avatar_service, booth_info_service


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


def test_fetch_info_targets_description_field_not_memo(client, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, _db = client
    fetched = booth_info_service.BoothProductInfo(
        name="Cool Outfit", shop_name="Cool Shop", shop_url=None, price=1000,
        image_url="https://example.com/thumb.png", description="A cool outfit description.",
    )
    monkeypatch.setattr(booth_info_service, "try_fetch_product_info", lambda url: fetched)

    response = test_client.get("/fragments/items/fetch-info", params={"product_url": "https://booth.pm/items/1"})

    assert response.status_code == 200
    assert 'id="f-description"' in response.text
    assert "A cool outfit description." in response.text
    assert 'id="f-memo"' not in response.text
    assert 'id="thumbnail-preview"' in response.text
    assert "https://example.com/thumb.png" in response.text


def _register_avatar(db: Session, name: str):
    item = Item(name=f"{name} base model")
    db.add(item)
    db.flush()
    return avatar_service.set_item_as_avatar(db, item.id, name=name, memo=None)


def test_fetch_info_checks_suggested_avatars_without_unchecking_existing(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, db = client
    manuka = _register_avatar(db, "Manuka")
    rusk = _register_avatar(db, "Rusk")

    fetched = booth_info_service.BoothProductInfo(
        name="Manuka Outfit", shop_name="Shop", shop_url=None, price=None, image_url=None,
        description="Manukaに対応した衣装です。",
    )
    monkeypatch.setattr(booth_info_service, "try_fetch_product_info", lambda url: fetched)

    response = test_client.get(
        "/fragments/items/fetch-info",
        params={"product_url": "https://booth.pm/items/2", "avatars": str(rusk.id)},
    )

    assert response.status_code == 200
    # Rusk was already checked in the live form (carried via hx-include) and
    # Manuka is newly suggested from the fetched text -- both must end up
    # checked in the OOB-replaced avatar list.
    assert response.text.count(f'value="{manuka.id}" checked') == 1
    assert response.text.count(f'value="{rusk.id}" checked') == 1


def test_fetch_info_does_not_suggest_tags(client, monkeypatch: pytest.MonkeyPatch) -> None:
    # 対応アバター is suggested from BOOTH text, but タグ is manual-only per spec.
    test_client, _db = client
    fetched = booth_info_service.BoothProductInfo(
        name="Item", shop_name="Shop", shop_url=None, price=None, image_url=None, description="desc"
    )
    monkeypatch.setattr(booth_info_service, "try_fetch_product_info", lambda url: fetched)

    response = test_client.get("/fragments/items/fetch-info", params={"product_url": "https://booth.pm/items/3"})

    assert response.status_code == 200
    assert "js-suggestion-chip" not in response.text
    assert "タグの候補" not in response.text
