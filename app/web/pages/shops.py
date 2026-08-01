from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import shop_service
from app.web.templating import templates

router = APIRouter()


@router.get("/shops")
def shops_page(request: Request, db: Session = Depends(get_db)):
    shops = shop_service.list_shops(db)
    return templates.TemplateResponse(request, "shops/list.html", {"shops": shops})
