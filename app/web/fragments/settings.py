from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_instance_config
from app.core.csrf import verify_csrf
from app.core.exceptions import NotFoundError
from app.db.session import get_db
from app.services import (
    booth_library_service,
    drive_reconcile_service,
    drive_sync_service,
    integrity_service,
    item_service,
    oauth_service,
)
from app.web.templating import templates

router = APIRouter(prefix="/fragments/settings", dependencies=[Depends(verify_csrf)])


@router.post("/sync-now")
async def sync_now(request: Request):
    # A manually-clicked "sync now" should always attempt a real sync --
    # including the self-heal check for a Drive DB file that's gone missing
    # -- even if nothing local has changed since the last push (the dirty
    # flag is in-memory only and resets on every process restart, so relying
    # on it alone would leave a user with no way to force a check).
    drive_sync_service.mark_dirty()
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


@router.post("/drive-reconcile")
async def drive_reconcile(request: Request, db: Session = Depends(get_db)):
    try:
        drive_client = oauth_service.make_drive_client(db)
    except oauth_service.NotConnectedError:
        return templates.TemplateResponse(
            request, "partials/drive_reconcile.html", {"error": "Google Driveが未接続です。", "result": None}
        )

    try:
        result = await run_in_threadpool(drive_reconcile_service.reconcile, db, drive_client)
    except Exception:
        return templates.TemplateResponse(
            request,
            "partials/drive_reconcile.html",
            {"error": "同期中にエラーが発生しました。時間をおいて再度お試しください。", "result": None},
            status_code=502,
        )
    return templates.TemplateResponse(request, "partials/drive_reconcile.html", {"result": result, "error": None})


@router.post("/booth-library/sync")
async def sync_booth_library(request: Request, db: Session = Depends(get_db)):
    config = get_instance_config()
    if not config.booth_library_cookie:
        return templates.TemplateResponse(
            request, "partials/booth_library_sync.html", {"message": "Cookieが未設定です。", "is_error": True}
        )

    try:
        count = await run_in_threadpool(booth_library_service.sync_library, db, config.booth_library_cookie)
    except booth_library_service.BoothSessionExpiredError:
        return templates.TemplateResponse(
            request,
            "partials/booth_library_sync.html",
            {"message": "セッションが無効です。Cookieを再設定してください。", "is_error": True},
        )
    except Exception:
        return templates.TemplateResponse(
            request,
            "partials/booth_library_sync.html",
            {"message": "同期中にエラーが発生しました。時間をおいて再度お試しください。", "is_error": True},
            status_code=502,
        )

    return templates.TemplateResponse(
        request,
        "partials/booth_library_sync.html",
        {"message": f"{count}件のファイルを同期しました。", "is_error": False},
    )


@router.post("/merge-duplicate-products")
async def merge_duplicate_products_now(request: Request, db: Session = Depends(get_db)):
    # Manual trigger for item_service.auto_merge_duplicate_products, which
    # otherwise only runs on its own 10-minute background sweep (merge_loop)
    # -- for confirming the result right away instead of waiting.
    merged = await run_in_threadpool(item_service.auto_merge_duplicate_products, db)
    message = f"{merged}件を統合しました。" if merged else "重複するBoothURLの商品はありませんでした。"
    return templates.TemplateResponse(request, "partials/merge_duplicate_products.html", {"message": message})


@router.post("/duplicate-files/scan")
async def scan_duplicate_files(request: Request, db: Session = Depends(get_db)):
    groups = await run_in_threadpool(item_service.find_duplicate_filename_groups, db)
    return templates.TemplateResponse(
        request, "partials/duplicate_files.html", {"groups": groups, "message": None}
    )


@router.post("/duplicate-files/merge")
async def merge_duplicate_files(request: Request, item_ids: list[int] = Form(...), db: Session = Depends(get_db)):
    try:
        await run_in_threadpool(item_service.merge_duplicate_group, db, item_ids)
    except NotFoundError:
        pass  # already merged/deleted by a concurrent request -- the re-scan below reflects current state either way
    groups = await run_in_threadpool(item_service.find_duplicate_filename_groups, db)
    return templates.TemplateResponse(
        request, "partials/duplicate_files.html", {"groups": groups, "message": "統合しました。"}
    )


@router.post("/missing-files/scan")
async def scan_missing_files(request: Request, db: Session = Depends(get_db)):
    items = await run_in_threadpool(item_service.find_items_without_file, db)
    return templates.TemplateResponse(request, "partials/missing_files.html", {"items": items, "message": None})


@router.post("/missing-files/delete/{item_id}")
async def delete_missing_file_item(request: Request, item_id: int, db: Session = Depends(get_db)):
    try:
        await run_in_threadpool(item_service.delete_item, db, item_id)
    except NotFoundError:
        pass  # already gone -- the re-scan below reflects current state either way
    items = await run_in_threadpool(item_service.find_items_without_file, db)
    return templates.TemplateResponse(
        request, "partials/missing_files.html", {"items": items, "message": "削除しました。"}
    )
