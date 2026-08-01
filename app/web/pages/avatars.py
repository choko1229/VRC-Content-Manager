from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import avatar_service
from app.web.templating import templates

router = APIRouter()


@router.get("/avatars")
def avatars_page(request: Request, db: Session = Depends(get_db)):
    avatars = avatar_service.list_avatar_options(db)
    return templates.TemplateResponse(request, "avatars/list.html", {"avatars": avatars})
