from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_db
from app.services import app_settings_service, drive_sync_service, oauth_service
from app.web.templating import templates

router = APIRouter()


@router.get("/setup")
def setup_page(request: Request):
    if not drive_sync_service.needs_setup():
        return RedirectResponse(url="/settings")
    return templates.TemplateResponse(request, "setup.html", {})


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    if drive_sync_service.needs_setup():
        return RedirectResponse(url="/setup")

    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "connected": oauth_service.is_connected(db),
            "last_pushed_at": app_settings_service.get_setting(db, "drive_db_last_pushed_at"),
            "drive_db_file_id": app_settings_service.get_setting(db, "drive_db_file_id"),
            "sync_interval_seconds": settings.sync_interval_seconds,
            "is_dirty": drive_sync_service.is_dirty(),
        },
    )
