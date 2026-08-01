from __future__ import annotations

import logging
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.core.csrf import verify_csrf
from app.core.exceptions import DriveError, NotFoundError
from app.core.validation import DEFAULT_ALLOWED_EXTENSIONS, UploadValidationError
from app.db.session import get_db
from app.models.item import Item
from app.models.status import Status
from app.schemas.item import ItemCreate, ItemSearchFilters
from app.services import (
    app_config_service,
    avatar_service,
    item_service,
    oauth_service,
    shop_service,
    tag_service,
    upload_service,
)
from app.services.upload_service import ValidatedUpload
from app.web.templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)

# Placeholder shop for the quick-upload flow (Google Drive-style: drop a file
# first, fill in the real shop/name/tags afterward in the sidebar).
UNASSIGNED_SHOP_NAME = "未設定"


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _items_list_context(
    db: Session, params, *, selected_item=None, upload_error: str | None = None
) -> dict:
    filters = ItemSearchFilters(
        keyword=params.get("q") or None,
        tags=_split_csv(params.get("tags", "")),
        avatars=_split_csv(params.get("avatars", "")),
        shop_id=int(params["shop_id"]) if params.get("shop_id") else None,
        status_code=params.get("status_code") or None,
        favorites_only=params.get("favorites_only") == "true",
    )
    items = item_service.search_items(db, filters)
    view = params.get("view", "table")
    shops = shop_service.list_shops(db)
    statuses = db.execute(select(Status).order_by(Status.sort_order)).scalars().all()
    return {
        "items": items,
        "view": view,
        "shop_options": [(str(s.id), s.name) for s in shops],
        "status_options": [(s.code, s.label) for s in statuses],
        "tag_names": tag_service.list_tag_names(db),
        "avatar_names": avatar_service.list_avatar_names(db),
        "filters": {
            "q": params.get("q", ""),
            "tags": params.get("tags", ""),
            "avatars": params.get("avatars", ""),
            "shop_id": params.get("shop_id", ""),
            "status_code": params.get("status_code", ""),
            "favorites_only": params.get("favorites_only") == "true",
        },
        "selected_item": selected_item,
        "upload_error": upload_error,
        "accept_extensions": ",".join(sorted(DEFAULT_ALLOWED_EXTENSIONS)),
        "allowed_extensions": ", ".join(sorted(DEFAULT_ALLOWED_EXTENSIONS)),
        "max_upload_size_mb": app_config_service.get_max_upload_size_mb(db),
    }


@router.get("/items")
def list_items_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "items/list.html",
        _items_list_context(db, request.query_params, upload_error=request.query_params.get("upload_error")),
    )


@router.get("/items/new")
def new_item_page() -> RedirectResponse:
    # The dedicated ingest page was folded into the TOP (/items) page's
    # upload button + whole-page drag-and-drop; this stays as a redirect so
    # old bookmarks/links still land somewhere useful.
    return RedirectResponse(url="/items")


def _derive_name_from_filename(filename: str) -> str:
    stem = Path(filename).stem.strip()
    return (stem or filename.strip() or "無題の商品")[:255]


@router.post("/items/new", dependencies=[Depends(verify_csrf)])
async def create_item(file: UploadFile | None = None, db: Session = Depends(get_db)):
    def _error_redirect(message: str) -> RedirectResponse:
        return RedirectResponse(url=f"/items?upload_error={quote(message)}", status_code=303)

    if file is None or not file.filename:
        return _error_redirect("ファイルを選択してください。")

    settings = get_settings()
    max_upload_size_mb = app_config_service.get_max_upload_size_mb(db)
    primary_upload: ValidatedUpload | None = None
    try:
        try:
            primary_upload = await upload_service.stream_and_validate_upload(
                file, dest_dir=settings.upload_tmp_dir, max_size_mb=max_upload_size_mb
            )
        except UploadValidationError as exc:
            return _error_redirect(str(exc))

        item_data = ItemCreate(
            name=_derive_name_from_filename(primary_upload.original_filename),
            shop_name=UNASSIGNED_SHOP_NAME,
        )
        try:
            created = await run_in_threadpool(
                item_service.create_item_with_file, db, data=item_data, primary_upload=primary_upload
            )
        except oauth_service.NotConnectedError:
            return _error_redirect("Google Driveが未接続です。先に設定から接続してください。")
        except DriveError as exc:
            logger.error("item ingest failed: %s", exc, exc_info=True)
            return _error_redirect(f"Google Driveへのアップロードに失敗しました: {exc}")
    finally:
        upload_service.cleanup_upload(primary_upload)

    return RedirectResponse(url=f"/items/{created.id}", status_code=303)


@router.get("/items/{item_id}")
def item_detail_page(request: Request, item_id: int, db: Session = Depends(get_db)):
    try:
        detail = item_service.get_item_detail(db, item_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="商品が見つかりません。")

    if request.headers.get("hx-request") == "true":
        return templates.TemplateResponse(request, "items/_detail_panel.html", {"item": detail})

    return templates.TemplateResponse(
        request, "items/list.html", _items_list_context(db, request.query_params, selected_item=detail)
    )


@router.get("/items/{item_id}/thumbnail")
def item_thumbnail(item_id: int, db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if item is None or item.thumbnail_file is None:
        raise HTTPException(status_code=404)
    thumb = item.thumbnail_file

    try:
        drive_client = oauth_service.make_drive_client(db)
    except oauth_service.NotConnectedError as exc:
        raise HTTPException(status_code=503, detail="Google Driveが未接続です。") from exc

    settings = get_settings()
    settings.upload_tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = settings.upload_tmp_dir / f"thumb_{uuid.uuid4().hex}"
    try:
        drive_client.download_file(file_id=thumb.drive_file_id, dest_path=tmp_path)
        content = tmp_path.read_bytes()
    except DriveError as exc:
        raise HTTPException(status_code=502, detail="サムネイルの取得に失敗しました。") from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    return Response(content=content, media_type=thumb.content_type or "application/octet-stream")


@router.get("/items/{item_id}/edit")
def edit_item_page_redirect(item_id: int) -> RedirectResponse:
    # Editing now happens inline in the detail sidebar (see
    # app/web/fragments/items.py: edit_item_panel_fragment) rather than on a
    # dedicated page; this stays as a redirect so old bookmarks/links still
    # land somewhere useful, matching the /items/new precedent above.
    return RedirectResponse(url=f"/items/{item_id}")
