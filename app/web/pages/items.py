from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.core.exceptions import DriveError
from app.core.validation import DEFAULT_ALLOWED_EXTENSIONS, UploadValidationError
from app.db.session import get_db
from app.models.status import Status
from app.schemas.item import ItemCreate
from app.services import avatar_service, item_service, oauth_service, shop_service, tag_service, upload_service
from app.services.upload_service import ValidatedUpload
from app.web.templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _form_context(db: Session, *, error: str | None = None, form_values: dict | None = None) -> dict:
    settings = get_settings()
    statuses = db.execute(select(Status).order_by(Status.sort_order)).scalars().all()
    return {
        "shops": [s.name for s in shop_service.list_shops(db)],
        "tag_names": tag_service.list_tag_names(db),
        "avatar_names": avatar_service.list_avatar_names(db),
        "statuses": statuses,
        "allowed_extensions": ", ".join(sorted(DEFAULT_ALLOWED_EXTENSIONS)),
        "accept_extensions": ",".join(sorted(DEFAULT_ALLOWED_EXTENSIONS)),
        "max_upload_size_mb": settings.max_upload_size_mb,
        "error": error,
        "form_values": form_values or {},
    }


@router.get("/items/new")
def new_item_page(request: Request, db: Session = Depends(get_db)):
    created_name = request.query_params.get("created")
    return templates.TemplateResponse(
        request,
        "items/ingest_form.html",
        {**_form_context(db), "created_name": created_name},
    )


@router.post("/items/new")
async def create_item(
    request: Request,
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
    file: UploadFile | None = None,
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
            "items/ingest_form.html",
            {**_form_context(db, error=message, form_values=form_values), "created_name": None},
            status_code=status_code_http,
        )

    if file is None or not file.filename:
        return _error_response("ファイルを選択してください。")

    try:
        item_data = ItemCreate(
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
        logger.warning("item create validation failed: %s", exc)
        return _error_response("入力内容を確認してください。")

    settings = get_settings()
    primary_upload: ValidatedUpload | None = None
    thumbnail_upload: ValidatedUpload | None = None
    try:
        try:
            primary_upload = await upload_service.stream_and_validate_upload(
                file, dest_dir=settings.upload_tmp_dir, max_size_mb=settings.max_upload_size_mb
            )
            if thumbnail is not None and thumbnail.filename:
                thumbnail_upload = await upload_service.stream_and_validate_upload(
                    thumbnail,
                    dest_dir=settings.upload_tmp_dir,
                    max_size_mb=settings.max_upload_size_mb,
                    allowed_extensions=frozenset({".png", ".jpg", ".jpeg"}),
                )
        except UploadValidationError as exc:
            return _error_response(str(exc))

        try:
            created = await run_in_threadpool(
                item_service.create_item_with_file,
                db,
                data=item_data,
                primary_upload=primary_upload,
                thumbnail_upload=thumbnail_upload,
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
        upload_service.cleanup_upload(thumbnail_upload)

    return RedirectResponse(url=f"/items/new?created={created.name}", status_code=303)
