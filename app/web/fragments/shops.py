from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.csrf import verify_csrf
from app.core.exceptions import NotFoundError
from app.db.session import get_db
from app.schemas.shop import ShopCreate
from app.services import booth_info_service, shop_service
from app.web.templating import templates

router = APIRouter(prefix="/fragments/shops", dependencies=[Depends(verify_csrf)])
logger = logging.getLogger(__name__)


def _table_response(request: Request, db: Session, status_code: int = 200, error: str | None = None):
    shops = shop_service.list_shops(db)
    return templates.TemplateResponse(
        request,
        "shops/_table.html",
        {"shops": shops, "error": error},
        status_code=status_code,
    )


@router.post("")
def create_shop_fragment(
    request: Request,
    name: str = Form(...),
    url: str = Form(""),
    memo: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        data = ShopCreate(name=name, url=url or None, memo=memo or None)
    except ValidationError as exc:
        logger.warning("shop create validation failed: %s", exc)
        return _table_response(request, db, status_code=422, error="入力内容を確認してください。")

    shop_service.create_shop(db, data)
    return _table_response(request, db)


@router.delete("/{shop_id}")
def delete_shop_fragment(request: Request, shop_id: int, db: Session = Depends(get_db)):
    try:
        shop_service.delete_shop(db, shop_id)
    except NotFoundError:
        return _table_response(request, db, status_code=404, error="対象のショップが見つかりません。")

    return _table_response(request, db)


@router.post("/{shop_id}/fetch-info")
async def fetch_shop_info_fragment(request: Request, shop_id: int, db: Session = Depends(get_db)):
    try:
        shop = shop_service.get_shop(db, shop_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="ショップが見つかりません。")

    error = None
    if not shop.url:
        error = "ショップURLが未設定です。"
    else:
        info = await run_in_threadpool(booth_info_service.try_fetch_shop_info, shop.url)
        if info is None:
            error = "ショップ情報を自動取得できませんでした。"
        else:
            shop_service.set_shop_fetched_info(db, shop_id, icon_url=info.icon_url, description=info.description)

    updated = next(s for s in shop_service.list_shops(db) if s.id == shop_id)
    return templates.TemplateResponse(request, "shops/_tile.html", {"shop": updated, "error": error})
