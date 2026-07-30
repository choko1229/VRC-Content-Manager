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
