from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.csrf import verify_csrf
from app.core.exceptions import NotFoundError
from app.db.session import get_db
from app.schemas.shop import ShopCreate
from app.services import shop_service
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
