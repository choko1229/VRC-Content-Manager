from __future__ import annotations

from fastapi import APIRouter, Request

from app.services import drive_sync_service
from app.web.templating import templates

router = APIRouter(prefix="/fragments/settings")


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
