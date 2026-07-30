from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.models.shop import Shop
from app.schemas.shop import ShopCreate, ShopRead, ShopUpdate

logger = logging.getLogger(__name__)


def _to_read(shop: Shop) -> ShopRead:
    return ShopRead(
        id=shop.id,
        name=shop.name,
        url=shop.url,
        memo=shop.memo,
        item_count=len(shop.items),
    )


def list_shops(db: Session) -> list[ShopRead]:
    shops = db.execute(select(Shop).order_by(Shop.name)).scalars().all()
    return [_to_read(shop) for shop in shops]


def get_shop(db: Session, shop_id: int) -> Shop:
    shop = db.get(Shop, shop_id)
    if shop is None:
        raise NotFoundError("Shop", shop_id)
    return shop


def create_shop(db: Session, data: ShopCreate) -> ShopRead:
    shop = Shop(name=data.name, url=data.url, memo=data.memo)
    db.add(shop)
    db.commit()
    db.refresh(shop)
    logger.info("shop created id=%s name=%s", shop.id, shop.name)
    return _to_read(shop)


def update_shop(db: Session, shop_id: int, data: ShopUpdate) -> ShopRead:
    shop = get_shop(db, shop_id)
    shop.name = data.name
    shop.url = data.url
    shop.memo = data.memo
    db.commit()
    db.refresh(shop)
    logger.info("shop updated id=%s", shop.id)
    return _to_read(shop)


def delete_shop(db: Session, shop_id: int) -> None:
    shop = get_shop(db, shop_id)
    db.delete(shop)
    db.commit()
    logger.info("shop deleted id=%s", shop_id)
