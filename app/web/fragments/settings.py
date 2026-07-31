from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.csrf import verify_csrf
from app.db.session import get_db
from app.services import drive_sync_service, integrity_service, oauth_service
from app.web.templating import templates

router = APIRouter(prefix="/fragments/settings", dependencies=[Depends(verify_csrf)])


@router.post("/sync-now")
async def sync_now(request: Request):
    synced = await drive_sync_service.flush_now_async()
    if synced:
        message = "同期しました。"
    elif drive_sync_service.is_dirty():
        message = "同期に失敗しました。ログを確認してください。"
    else:
        message = "変更はありません(同期不要)。"
    return templates.TemplateResponse(
        request,
        "partials/sync_status.html",
        {"message": message, "is_dirty": drive_sync_service.is_dirty()},
    )


@router.post("/integrity-check")
async def integrity_check(request: Request, db: Session = Depends(get_db)):
    try:
        drive_client = oauth_service.make_drive_client(db)
    except oauth_service.NotConnectedError:
        return templates.TemplateResponse(
            request, "partials/integrity_check.html", {"error": "Google Driveが未接続です。", "broken": None}
        )

    broken = await run_in_threadpool(integrity_service.check_for_broken_references, db, drive_client)
    return templates.TemplateResponse(request, "partials/integrity_check.html", {"broken": broken, "error": None})
