from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError
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
from app.schemas.item import ItemCreate, ItemSearchFilters, ItemUpdate
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
# first, fill in the real shop/name/tags afterward on the edit page).
UNASSIGNED_SHOP_NAME = "未設定"
THUMBNAIL_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _form_context(db: Session, *, error: str | None = None, form_values: dict | None = None) -> dict:
    statuses = db.execute(select(Status).order_by(Status.sort_order)).scalars().all()
    return {
        "shops": [s.name for s in shop_service.list_shops(db)],
        "tag_names": tag_service.list_tag_names(db),
        "avatar_names": avatar_service.list_avatar_names(db),
        "status_options": [(s.code, s.label) for s in statuses],
        "allowed_extensions": ", ".join(sorted(DEFAULT_ALLOWED_EXTENSIONS)),
        "accept_extensions": ",".join(sorted(DEFAULT_ALLOWED_EXTENSIONS)),
        "max_upload_size_mb": app_config_service.get_max_upload_size_mb(db),
        "error": error,
        "form_values": form_values or {},
    }


def _quick_upload_context(db: Session, *, error: str | None = None) -> dict:
    return {
        "allowed_extensions": ", ".join(sorted(DEFAULT_ALLOWED_EXTENSIONS)),
        "accept_extensions": ",".join(sorted(DEFAULT_ALLOWED_EXTENSIONS)),
        "max_upload_size_mb": app_config_service.get_max_upload_size_mb(db),
        "error": error,
    }


@router.get("/items/new")
def new_item_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "items/ingest_form.html", _quick_upload_context(db))


def _derive_name_from_filename(filename: str) -> str:
    stem = Path(filename).stem.strip()
    return (stem or filename.strip() or "無題の商品")[:255]


@router.post("/items/new", dependencies=[Depends(verify_csrf)])
async def create_item(
    request: Request,
    file: UploadFile | None = None,
    db: Session = Depends(get_db),
):
    def _error_response(message: str, status_code_http: int = 422):
        return templates.TemplateResponse(
            request,
            "items/ingest_form.html",
            _quick_upload_context(db, error=message),
            status_code=status_code_http,
        )

    if file is None or not file.filename:
        return _error_response("ファイルを選択してください。")

    settings = get_settings()
    max_upload_size_mb = app_config_service.get_max_upload_size_mb(db)
    primary_upload: ValidatedUpload | None = None
    try:
        try:
            primary_upload = await upload_service.stream_and_validate_upload(
                file, dest_dir=settings.upload_tmp_dir, max_size_mb=max_upload_size_mb
            )
        except UploadValidationError as exc:
            return _error_response(str(exc))

        item_data = ItemCreate(
            name=_derive_name_from_filename(primary_upload.original_filename),
            shop_name=UNASSIGNED_SHOP_NAME,
        )
        try:
            created = await run_in_threadpool(
                item_service.create_item_with_file, db, data=item_data, primary_upload=primary_upload
            )
        except oauth_service.NotConnectedError:
            return _error_response(
                "Google Driveが未接続です。先に /settings から接続してください。", status_code_http=409
            )
        except DriveError as exc:
            logger.error("item ingest failed: %s", exc, exc_info=True)
            return _error_response(f"Google Driveへのアップロードに失敗しました: {exc}", status_code_http=502)
    finally:
        upload_service.cleanup_upload(primary_upload)

    return RedirectResponse(url=f"/items/{created.id}/edit?uploaded=1", status_code=303)


@router.get("/items")
def list_items_page(request: Request, db: Session = Depends(get_db)):
    params = request.query_params
    filters = ItemSearchFilters(
        keyword=params.get("q") or None,
        tags=_split_csv(params.get("tags", "")),
        avatars=_split_csv(params.get("avatars", "")),
        shop_id=int(params["shop_id"]) if params.get("shop_id") else None,
        status_code=params.get("status_code") or None,
        favorites_only=params.get("favorites_only") == "true",
    )
    items = item_service.search_items(db, filters)
    view = params.get("view", "card")
    shops = shop_service.list_shops(db)
    statuses = db.execute(select(Status).order_by(Status.sort_order)).scalars().all()
    return templates.TemplateResponse(
        request,
        "items/list.html",
        {
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
        },
    )


@router.get("/items/{item_id}")
def item_detail_page(request: Request, item_id: int, db: Session = Depends(get_db)):
    try:
        detail = item_service.get_item_detail(db, item_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="商品が見つかりません。")
    return templates.TemplateResponse(request, "items/detail.html", {"item": detail})


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


def _edit_form_context(db: Session, item_id: int, *, error: str | None = None, form_values: dict | None = None) -> dict:
    return {**_form_context(db, error=error, form_values=form_values), "item_id": item_id}


@router.get("/items/{item_id}/edit")
def edit_item_page(request: Request, item_id: int, db: Session = Depends(get_db)):
    try:
        detail = item_service.get_item_detail(db, item_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="商品が見つかりません。")

    just_uploaded = request.query_params.get("uploaded") == "1"
    form_values = {
        "name": detail.name,
        "shop_name": detail.shop_name or "",
        "shop_url": detail.shop_url or "",
        "product_url": detail.product_url or "",
        "download_source_url": detail.download_source_url or "",
        "purchase_date": detail.purchase_date.isoformat() if detail.purchase_date else "",
        "download_date": detail.download_date.isoformat() if detail.download_date else "",
        "price": str(detail.price) if detail.price is not None else "",
        "status_code": detail.status_code or "",
        "memo": detail.memo or "",
        "is_favorite": detail.is_favorite,
        "tags": ", ".join(detail.tags),
        "avatars": ", ".join(detail.avatars),
        "commercial_use": detail.commercial_use.value,
        "modification_allowed": detail.modification_allowed.value,
        "redistribution_allowed": detail.redistribution_allowed.value,
        "credit_required": detail.credit_required.value,
        "license_note": detail.license_note or "",
    }
    return templates.TemplateResponse(
        request,
        "items/edit_form.html",
        {
            **_edit_form_context(db, item_id, form_values=form_values),
            "just_uploaded": just_uploaded,
            "has_thumbnail": detail.has_thumbnail,
        },
    )


@router.post("/items/{item_id}/edit", dependencies=[Depends(verify_csrf)])
async def submit_edit_item(
    request: Request,
    item_id: int,
    name: str = Form(...),
    shop_name: str = Form(...),
    shop_url: str = Form(""),
    product_url: str = Form(""),
    download_source_url: str = Form(""),
    purchase_date: str = Form(""),
    download_date: str = Form(""),
    price: str = Form(""),
    status_code: str = Form(""),
    memo: str = Form(""),
    is_favorite: bool = Form(False),
    tags: str = Form(""),
    avatars: str = Form(""),
    commercial_use: str = Form("unknown"),
    modification_allowed: str = Form("unknown"),
    redistribution_allowed: str = Form("unknown"),
    credit_required: str = Form("unknown"),
    license_note: str = Form(""),
    thumbnail: UploadFile | None = None,
    db: Session = Depends(get_db),
):
    form_values = {
        "name": name,
        "shop_name": shop_name,
        "shop_url": shop_url,
        "product_url": product_url,
        "download_source_url": download_source_url,
        "purchase_date": purchase_date,
        "download_date": download_date,
        "price": price,
        "status_code": status_code,
        "memo": memo,
        "is_favorite": is_favorite,
        "tags": tags,
        "avatars": avatars,
        "commercial_use": commercial_use,
        "modification_allowed": modification_allowed,
        "redistribution_allowed": redistribution_allowed,
        "credit_required": credit_required,
        "license_note": license_note,
    }

    def _error_response(message: str, status_code_http: int = 422):
        return templates.TemplateResponse(
            request,
            "items/edit_form.html",
            _edit_form_context(db, item_id, error=message, form_values=form_values),
            status_code=status_code_http,
        )

    try:
        item_data = ItemUpdate(
            name=name,
            shop_name=shop_name,
            shop_url=shop_url or None,
            product_url=product_url or None,
            download_source_url=download_source_url or None,
            purchase_date=purchase_date or None,
            download_date=download_date or None,
            price=int(price) if price.strip() else None,
            status_code=status_code or None,
            memo=memo or None,
            is_favorite=is_favorite,
            tags=_split_csv(tags),
            avatars=_split_csv(avatars),
            commercial_use=commercial_use,
            modification_allowed=modification_allowed,
            redistribution_allowed=redistribution_allowed,
            credit_required=credit_required,
            license_note=license_note or None,
        )
    except (ValidationError, ValueError) as exc:
        logger.warning("item update validation failed: %s", exc)
        return _error_response("入力内容を確認してください。")

    settings = get_settings()
    max_upload_size_mb = app_config_service.get_max_upload_size_mb(db)
    thumbnail_upload: ValidatedUpload | None = None
    try:
        if thumbnail is not None and thumbnail.filename:
            try:
                thumbnail_upload = await upload_service.stream_and_validate_upload(
                    thumbnail,
                    dest_dir=settings.upload_tmp_dir,
                    max_size_mb=max_upload_size_mb,
                    allowed_extensions=THUMBNAIL_EXTENSIONS,
                )
            except UploadValidationError as exc:
                return _error_response(str(exc))

        try:
            await run_in_threadpool(
                item_service.update_item, db, item_id, item_data, thumbnail_upload=thumbnail_upload
            )
        except NotFoundError:
            raise HTTPException(status_code=404, detail="商品が見つかりません。")
    finally:
        upload_service.cleanup_upload(thumbnail_upload)

    return RedirectResponse(url=f"/items/{item_id}", status_code=303)
