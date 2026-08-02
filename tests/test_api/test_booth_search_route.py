"""Coverage for /fragments/items/booth-search: guesses a BOOTH product page
from an item's (filename-derived) name (see app/web/fragments/items.py:
booth_search_fragment) and for how the edit panel wires it in."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.main import app
from app.schemas.item import ItemCreate
from app.services import booth_info_service, item_service
from app.services.upload_service import ValidatedUpload


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


def _make_upload(tmp_path: Path, name: str, content: bytes = b"dummy") -> ValidatedUpload:
    path = tmp_path / name
    path.write_bytes(content)
    return ValidatedUpload(
        path=path, original_filename=name, size_bytes=len(content), content_type="application/zip", extension=".zip"
    )


def test_booth_search_route_renders_suggestion_cards(client, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, _db = client
    results = [
        booth_info_service.BoothSearchResult(
            product_url="https://booth.pm/ja/items/123",
            name="Cool Avatar",
            shop_name="Cool Shop",
            thumbnail_url="https://booth.pximg.net/thumb.jpg",
        )
    ]
    monkeypatch.setattr(booth_info_service, "search_products", lambda q, **kw: results)

    response = test_client.get("/fragments/items/booth-search", params={"q": "Cool Avatar"})

    assert response.status_code == 200
    assert "js-booth-suggestion" in response.text
    assert 'data-product-url="https://booth.pm/ja/items/123"' in response.text
    assert "Cool Avatar" in response.text
    assert "Cool Shop" in response.text


def test_booth_search_route_renders_nothing_for_no_results(client, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, _db = client
    monkeypatch.setattr(booth_info_service, "search_products", lambda q, **kw: [])

    response = test_client.get("/fragments/items/booth-search", params={"q": "no match"})

    assert response.status_code == 200
    assert "js-booth-suggestion" not in response.text
    assert 'id="booth-suggestions"' in response.text  # placeholder still present for future OOB swaps


def test_booth_search_route_handles_missing_query(client) -> None:
    test_client, _db = client

    response = test_client.get("/fragments/items/booth-search")

    assert response.status_code == 200
    assert "js-booth-suggestion" not in response.text


def test_edit_panel_shows_a_manual_filename_search_button(client, tmp_path: Path) -> None:
    """The search is user-triggered (a button), not auto-run on load -- see
    items/_edit_panel.html. hx-target is an explicit id selector here, not
    "this", so it's unaffected by the enclosing <form>'s own
    hx-target="this"/hx-swap="none" (which would otherwise be inherited)."""
    test_client, db = client
    created = item_service.create_item_with_file(
        db, data=ItemCreate(name="Unlinked Item", shop_name="未設定"), primary_upload=_make_upload(tmp_path, "a.zip")
    )

    response = test_client.get(f"/fragments/items/{created.id}/edit")

    assert response.status_code == 200
    assert "ファイル名で検索" in response.text
    assert 'hx-get="/fragments/items/booth-search?q=Unlinked' in response.text
    assert 'hx-target="#booth-suggestions"' in response.text


def test_edit_panel_shows_the_search_button_even_when_already_linked(client, tmp_path: Path) -> None:
    """Unlike the old auto-search-on-load behavior (which only ever ran for
    an unlinked item), the manual button stays available after a BoothURL
    is already set too -- useful to re-search or double-check a match."""
    test_client, db = client
    created = item_service.create_item_with_file(
        db, data=ItemCreate(name="Linked Item", shop_name="未設定", product_url="https://booth.pm/ja/items/9"),
        primary_upload=_make_upload(tmp_path, "b.zip"),
    )

    response = test_client.get(f"/fragments/items/{created.id}/edit")

    assert response.status_code == 200
    assert 'hx-get="/fragments/items/booth-search?q=Linked' in response.text


def test_fetch_info_result_clears_suggestions_on_successful_fetch(client, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, _db = client
    fetched = booth_info_service.BoothProductInfo(
        name="Item", shop_name="Shop", shop_url=None, price=None, image_url=None, description=None
    )
    monkeypatch.setattr(booth_info_service, "try_fetch_product_info", lambda url: fetched)

    response = test_client.get("/fragments/items/fetch-info", params={"product_url": "https://booth.pm/items/1"})

    assert response.status_code == 200
    assert 'id="booth-suggestions" hx-swap-oob="true"' in response.text
