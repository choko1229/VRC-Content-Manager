from __future__ import annotations

import asyncio
import logging
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
from app.models.item import Item, ItemCategory
from app.models.status import Status
from app.schemas.item import ItemCreate, ItemRead, ItemSearchFilters
from app.services import (
    app_config_service,
    avatar_service,
    chunked_upload_service,
    file_content_service,
    item_service,
    oauth_service,
    saved_filter_service,
    shop_service,
    tag_service,
    upload_service,
    upload_sync_service,
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
        category=ItemCategory(params["category"]) if params.get("category") else None,
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
        "category_options": item_service.CATEGORY_OPTIONS,
        "tag_names": tag_service.list_tag_names(db),
        "avatar_names": avatar_service.list_avatar_names(db),
        "saved_filters": saved_filter_service.list_saved_filters(db),
        "filters": {
            "q": params.get("q", ""),
            "tags": params.get("tags", ""),
            "avatars": params.get("avatars", ""),
            "shop_id": params.get("shop_id", ""),
            "status_code": params.get("status_code", ""),
            "category": params.get("category", ""),
            "favorites_only": params.get("favorites_only") == "true",
        },
        "selected_item": selected_item,
        "upload_error": upload_error,
        "accept_extensions": ",".join(sorted(DEFAULT_ALLOWED_EXTENSIONS)),
        "allowed_extensions": ", ".join(sorted(DEFAULT_ALLOWED_EXTENSIONS)),
        "max_upload_size_mb": app_config_service.get_max_upload_size_mb(db),
        "chunk_size_mb": chunked_upload_service.CHUNK_SIZE_MB,
        "pending_sync_count": item_service.count_pending_sync(db),
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


async def _create_item_from_upload(db: Session, primary_upload: ValidatedUpload) -> tuple[ItemRead, Item | None]:
    """Shared by both upload paths (single-request and chunked, see
    create_item / create_item_chunked_complete below): creates the
    minimally-populated item, then fires the background Drive push.

    Also checks for another item whose primary file already has this exact
    filename -- rather than silently leaving two library entries for what's
    very likely the same product uploaded twice, the caller surfaces this
    back to the client so it can offer a merge (see items/list.html's
    duplicate-confirm toast and /fragments/items/{id}/merge-duplicate-into/{id}).
    A filename match isn't unambiguous enough to merge automatically, so the
    new item is still created either way -- this is purely advisory.
    """
    item_data = ItemCreate(
        name=_derive_name_from_filename(primary_upload.original_filename),
        shop_name=UNASSIGNED_SHOP_NAME,
    )
    created = await run_in_threadpool(
        item_service.create_item_with_file, db, data=item_data, primary_upload=primary_upload
    )
    # Fire-and-forget: push the newly-cached file to Drive in the background
    # instead of making the upload response wait on it (see
    # upload_sync_service). The periodic sweep would pick it up anyway --
    # this just makes it feel instant.
    asyncio.create_task(asyncio.to_thread(upload_sync_service.sync_pending_now))
    duplicate = await run_in_threadpool(
        item_service.find_duplicate_filename_item,
        db,
        primary_upload.original_filename,
        exclude_item_id=created.id,
    )
    return created, duplicate


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

        created, duplicate = await _create_item_from_upload(db, primary_upload)
    finally:
        upload_service.cleanup_upload(primary_upload)

    url = f"/items/{created.id}"
    if duplicate is not None:
        url += f"?duplicate_of={duplicate.id}&duplicate_name={quote(duplicate.name)}"
    return RedirectResponse(url=url, status_code=303)


# Chunked counterpart to POST /items/new above, used by the frontend for any
# file larger than chunked_upload_service.CHUNK_SIZE_MB -- see that module's
# docstring for why: a proxy/CDN in front of the app (Cloudflare Tunnel on
# Free/Pro, notably) can enforce a hard ~100MB cap on a single request body
# that no app-side setting can raise, so a large file has to arrive as
# several smaller requests instead of one.
@router.post("/items/new/chunked/init", dependencies=[Depends(verify_csrf)])
async def create_item_chunked_init(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    filename = str(body.get("filename") or "")
    total_size = int(body.get("size") or 0)
    max_upload_size_mb = app_config_service.get_max_upload_size_mb(db)

    try:
        upload_id = await run_in_threadpool(
            chunked_upload_service.init_session,
            filename=filename,
            total_size=total_size,
            max_size_mb=max_upload_size_mb,
        )
    except UploadValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {"upload_id": upload_id, "chunk_size_mb": chunked_upload_service.CHUNK_SIZE_MB}


@router.post("/items/new/chunked/{upload_id}/complete", dependencies=[Depends(verify_csrf)])
async def create_item_chunked_complete(upload_id: str, db: Session = Depends(get_db)):
    try:
        primary_upload = await run_in_threadpool(chunked_upload_service.complete_session, upload_id)
    except UploadValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        created, duplicate = await _create_item_from_upload(db, primary_upload)
    finally:
        upload_service.cleanup_upload(primary_upload)

    response = {"item_id": created.id}
    if duplicate is not None:
        response["duplicate_of"] = duplicate.id
        response["duplicate_name"] = duplicate.name
    return response


# Registered after the /complete route above on purpose: {chunk_index} is a
# generic path capture that would otherwise also match the literal
# "complete" segment, and Starlette matches routes in registration order --
# with this one first, a POST to .../complete would 422 on "complete" not
# being a valid int instead of ever reaching create_item_chunked_complete.
@router.post("/items/new/chunked/{upload_id}/{chunk_index}", dependencies=[Depends(verify_csrf)])
async def create_item_chunked_part(upload_id: str, chunk_index: int, request: Request):
    try:
        await chunked_upload_service.write_chunk(upload_id, chunk_index, request)
    except UploadValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"received": chunk_index}


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

    drive_client = None
    if thumb.synced_at is not None:
        try:
            drive_client = oauth_service.make_drive_client(db)
        except oauth_service.NotConnectedError as exc:
            raise HTTPException(status_code=503, detail="Google Driveが未接続です。") from exc

    try:
        path = file_content_service.resolve_local_path(thumb, drive_client)
    except DriveError as exc:
        raise HTTPException(status_code=502, detail="サムネイルの取得に失敗しました。") from exc

    return Response(content=path.read_bytes(), media_type=thumb.content_type or "application/octet-stream")


@router.get("/items/{item_id}/edit")
def edit_item_page_redirect(item_id: int) -> RedirectResponse:
    # Editing now happens inline in the detail sidebar (see
    # app/web/fragments/items.py: edit_item_panel_fragment) rather than on a
    # dedicated page; this stays as a redirect so old bookmarks/links still
    # land somewhere useful, matching the /items/new precedent above.
    return RedirectResponse(url=f"/items/{item_id}")
