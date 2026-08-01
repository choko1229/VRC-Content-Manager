from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.schemas.shop import ShopCreate, ShopUpdate
from app.services import shop_service


def test_create_and_list_shop(db_session: Session) -> None:
    shop_service.create_shop(db_session, ShopCreate(name="Test Shop", url="https://example.com"))

    shops = shop_service.list_shops(db_session)

    assert len(shops) == 1
    assert shops[0].name == "Test Shop"
    assert shops[0].item_count == 0


def test_create_shop_strips_and_rejects_whitespace_only_name() -> None:
    with pytest.raises(ValidationError):
        ShopCreate(name="   ")


def test_create_shop_strips_surrounding_whitespace(db_session: Session) -> None:
    shop_service.create_shop(db_session, ShopCreate(name="  Padded Name  "))

    shops = shop_service.list_shops(db_session)

    assert shops[0].name == "Padded Name"


def test_update_shop(db_session: Session) -> None:
    created = shop_service.create_shop(db_session, ShopCreate(name="Original"))

    updated = shop_service.update_shop(db_session, created.id, ShopUpdate(name="Renamed"))

    assert updated.name == "Renamed"


def test_update_missing_shop_raises_not_found(db_session: Session) -> None:
    with pytest.raises(NotFoundError):
        shop_service.update_shop(db_session, 999, ShopUpdate(name="Doesn't matter"))


def test_delete_shop(db_session: Session) -> None:
    created = shop_service.create_shop(db_session, ShopCreate(name="To Delete"))

    shop_service.delete_shop(db_session, created.id)

    assert shop_service.list_shops(db_session) == []


def test_delete_missing_shop_raises_not_found(db_session: Session) -> None:
    with pytest.raises(NotFoundError):
        shop_service.delete_shop(db_session, 999)


def test_get_or_create_shop_creates_when_absent(db_session: Session) -> None:
    shop = shop_service.get_or_create_shop(db_session, name="New Shop", url="https://example.com")

    assert shop.id is not None
    assert shop.name == "New Shop"


def test_get_or_create_shop_reuses_existing_by_name(db_session: Session) -> None:
    created = shop_service.create_shop(db_session, ShopCreate(name="Existing Shop", url="https://a.example"))

    reused = shop_service.get_or_create_shop(db_session, name="Existing Shop", url="https://different.example")

    assert reused.id == created.id
    assert reused.url == "https://a.example"  # existing shop's URL is not overwritten


def test_set_shop_fetched_info_fills_icon_and_empty_memo(db_session: Session) -> None:
    created = shop_service.create_shop(db_session, ShopCreate(name="Fetchable Shop"))

    updated = shop_service.set_shop_fetched_info(
        db_session, created.id, icon_url="https://example.com/icon.png", description="お店の説明"
    )

    assert updated.icon_url == "https://example.com/icon.png"
    assert updated.memo == "お店の説明"


def test_set_shop_fetched_info_does_not_overwrite_existing_memo(db_session: Session) -> None:
    created = shop_service.create_shop(db_session, ShopCreate(name="Has Memo", memo="自分で書いたメモ"))

    updated = shop_service.set_shop_fetched_info(
        db_session, created.id, icon_url="https://example.com/icon.png", description="Boothから取得した説明"
    )

    assert updated.memo == "自分で書いたメモ"
    assert updated.icon_url == "https://example.com/icon.png"
