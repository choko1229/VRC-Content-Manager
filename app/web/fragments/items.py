from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.core.csrf import verify_csrf
from app.core.exceptions import NotFoundError
from app.core.validation import UploadValidationError
from app.db.session import get_db
from app.models.item import ItemCategory
from app.schemas.item import ItemSearchFilters, ItemUpdate
from app.services import (
    app_config_service,
    avatar_service,
    booth_info_service,
    item_service,
    shop_service,
    tag_service,
    upload_service,
    upload_sync_service,
)
from app.services.upload_service import ValidatedUpload
from app.web.templating import templates

router = APIRouter(prefix="/fragments/items")
logger = logging.getLogger(__name__)

THUMBNAIL_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


_EMPTY_FETCH_INFO_CONTEXT = {
    "info": None,
    "error": None,
    "avatar_options": [],
    "checked_ids": set(),
    "has_suggested_avatars": False,
}


@router.get("/fetch-info")
async def fetch_info_fragment(request: Request, product_url: str = "", db: Session = Depends(get_db)):
    product_url = product_url.strip()
    avatar_options = avatar_service.list_avatar_options(db)
    avatars_by_name = {a.name: a for a in avatar_options}
    # hx-include="closest form" on the BoothURL field resends every current
    # form value (including already-checked avatar checkboxes, as ids) as
    # query params, so a fetch mid-edit doesn't clobber avatars the user
    # already picked -- the OOB avatar list below is a union, never a reset.
    already_checked = {int(v) for v in request.query_params.getlist("avatars") if v.isdigit()}

    if not product_url:
        return templates.TemplateResponse(
            request,
            "items/_fetch_info_result.html",
            {**_EMPTY_FETCH_INFO_CONTEXT, "avatar_options": avatar_options, "checked_ids": already_checked},
        )

    info = await run_in_threadpool(booth_info_service.try_fetch_product_info, product_url)
    if info is None:
        return templates.TemplateResponse(
            request,
            "items/_fetch_info_result.html",
            {
                **_EMPTY_FETCH_INFO_CONTEXT,
                "avatar_options": avatar_options,
                "checked_ids": already_checked,
                "error": "商品情報を自動取得できませんでした。お手数ですが手動で入力してください。",
            },
        )

    suggested_names = booth_info_service.match_known_terms(list(avatars_by_name), info.name, info.description)
    suggested_ids = {avatars_by_name[name].id for name in suggested_names[:10]}
    return templates.TemplateResponse(
        request,
        "items/_fetch_info_result.html",
        {
            "info": info,
            "error": None,
            "avatar_options": avatar_options,
            "checked_ids": already_checked | suggested_ids,
            "has_suggested_avatars": bool(suggested_ids),
        },
    )


@router.get("/booth-search")
async def booth_search_fragment(request: Request, q: str = ""):
    # Best-effort candidate list for an item that isn't linked to a BOOTH
    # page yet -- see items/_edit_panel.html, which triggers this on load
    # using the item's (filename-derived) name as the query, and
    # booth_info_service.search_products for the low-risk fetch policy.
    results = await run_in_threadpool(booth_info_service.search_products, q)
    return templates.TemplateResponse(request, "items/_booth_search_result.html", {"results": results})


@router.get("")
def search_items_fragment(request: Request, db: Session = Depends(get_db)):
    params = request.query_params
    filters = ItemSearchFilters(
        keyword=params.get("q") or None,
        tags=_split_csv(params.get("tags", "")),
        avatars=_split_csv(params.get("avatars", "")),
        shop_id=int(params["shop_id"]) if params.get("shop_id") else None,
        status_code=params.get("status_code") or None,
        category=ItemCategory(params["category"]) if params.get("category") else None,
        favorites_only=params.get("favorites_only") == "true",
    )
    view = params.get("view", "table")
    items = item_service.search_items(db, filters)
    template = "items/_results_table.html" if view == "table" else "items/_results_card.html"
    return templates.TemplateResponse(request, template, {"items": items})


@router.post("/bulk-update", dependencies=[Depends(verify_csrf)])
def bulk_update_items_fragment(
    item_ids: str = Form(...),
    status_code: str = Form(""),
    add_tags: str = Form(""),
    add_avatars: str = Form(""),
    favorite: str = Form(""),
    db: Session = Depends(get_db),
):
    ids = [int(part) for part in item_ids.split(",") if part.strip().isdigit()]
    is_favorite = {"true": True, "false": False}.get(favorite)
    updated = item_service.bulk_update(
        db,
        ids,
        status_code=status_code or None,
        add_tag_names=_split_csv(add_tags),
        add_avatar_names=_split_csv(add_avatars),
        is_favorite=is_favorite,
    )
    return {"updated": updated}


@router.delete("/{item_id}", dependencies=[Depends(verify_csrf)])
def delete_item_fragment(request: Request, item_id: int, db: Session = Depends(get_db)):
    try:
        item_service.delete_item(db, item_id)
    except NotFoundError:
        pass  # already gone -- collapsing back to the empty state is still the right outcome
    return templates.TemplateResponse(request, "items/_detail_empty.html", {})


@router.post("/{item_id}/update-check", dependencies=[Depends(verify_csrf)])
def add_update_check_fragment(
    request: Request, item_id: int, note: str = Form(""), db: Session = Depends(get_db)
):
    try:
        item_service.add_update_check(db, item_id, note or None)
    except NotFoundError:
        return templates.TemplateResponse(
            request, "partials/update_history.html", {"history": [], "error": "アイテムが見つかりません。"}, status_code=404
        )

    detail = item_service.get_item_detail(db, item_id)
    return templates.TemplateResponse(request, "partials/update_history.html", {"history": detail.update_history})


def _edit_panel_context(
    db: Session,
    item_id: int,
    item_name: str,
    has_thumbnail: bool,
    *,
    form_values: dict,
    checked_avatar_ids: set[int],
    is_avatar: bool,
    avatar_form_values: dict,
    error: str | None = None,
) -> dict:
    return {
        "item_id": item_id,
        "item_name": item_name,
        "has_thumbnail": has_thumbnail,
        "shops": [s.name for s in shop_service.list_shops(db)],
        "tag_names": tag_service.list_tag_names(db),
        "avatar_options": avatar_service.list_avatar_options(db),
        "checked_avatar_ids": checked_avatar_ids,
        "is_avatar": is_avatar,
        "avatar_form_values": avatar_form_values,
        "form_values": form_values,
        "category_options": item_service.CATEGORY_OPTIONS,
        "error": error,
    }


@router.get("/{item_id}/edit")
def edit_item_panel_fragment(request: Request, item_id: int, db: Session = Depends(get_db)):
    try:
        detail = item_service.get_item_detail(db, item_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="商品が見つかりません。")

    form_values = {
        "product_url": detail.product_url or "",
        "name": detail.name,
        "category": detail.category.value,
        "shop_name": detail.shop_name or "",
        "description": detail.description or "",
        "tags": ", ".join(detail.tags),
        "memo": detail.memo or "",
    }
    current_avatar = avatar_service.get_avatar_for_item(db, item_id)
    if current_avatar is not None:
        avatar_form_values = {"name": current_avatar.name, "memo": current_avatar.memo or ""}
    else:
        avatar_form_values = {"name": "", "memo": ""}
    checked_avatar_ids = {a.id for a in avatar_service.resolve_existing_avatars(db, detail.avatars)}
    context = _edit_panel_context(
        db,
        item_id,
        detail.name,
        detail.has_thumbnail,
        form_values=form_values,
        checked_avatar_ids=checked_avatar_ids,
        is_avatar=current_avatar is not None,
        avatar_form_values=avatar_form_values,
    )
    return templates.TemplateResponse(request, "items/_edit_panel.html", context)


@router.post("/{item_id}/edit", dependencies=[Depends(verify_csrf)])
async def submit_edit_item_panel_fragment(
    request: Request,
    item_id: int,
    product_url: str = Form(""),
    name: str = Form(...),
    category: str = Form(ItemCategory.CLOTHING.value),
    shop_name: str = Form(...),
    description: str = Form(""),
    tags: str = Form(""),
    avatars: list[int] = Form([]),
    memo: str = Form(""),
    is_avatar: bool = Form(False),
    avatar_name: str = Form(""),
    avatar_memo: str = Form(""),
    force_duplicate: bool = Form(False),
    thumbnail: UploadFile | None = None,
    db: Session = Depends(get_db),
):
    """Auto-save: the sidebar form (items/_edit_panel.html) submits here on
    every focusout, not a single explicit "save" click. The form itself
    uses hx-swap="none" so a save round-trip never touches its own DOM
    (preserving focus/scroll/typed values mid-edit) -- the response is
    always the small OOB-only status template, and always HTTP 200 (even
    on a validation failure) so htmx still processes those OOB updates;
    htmx skips OOB processing on non-2xx responses by default. Only a
    truly broken state (the item itself no longer exists) is a real error.
    """

    def _status(message: str, *, error: bool, item_name: str | None = None, thumbnail_url: str | None = None):
        return templates.TemplateResponse(
            request,
            "items/_autosave_status.html",
            {"message": message, "error": error, "item_name": item_name, "thumbnail_url": thumbnail_url},
        )

    try:
        current = item_service.get_item_detail(db, item_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="商品が見つかりません。")

    # A newly-entered (or changed) BoothURL that another item already links
    # to offers a merge instead of silently letting two items point at the
    # same product -- see items/_duplicate_confirm.html. force_duplicate is
    # only ever set by that banner's own "別々のまま登録" button, bypassing
    # this check for the one save that resolves it.
    normalized_url = product_url or None
    if normalized_url and normalized_url != current.product_url and not force_duplicate:
        duplicate = item_service.find_duplicate_product_url_item(db, normalized_url, exclude_item_id=item_id)
        if duplicate is not None:
            return templates.TemplateResponse(
                request,
                "items/_duplicate_confirm.html",
                {"item_id": item_id, "existing_item_id": duplicate.id, "existing_item_name": duplicate.name},
            )

    # 対応アバター checkboxes only ever offer avatars that already exist
    # (avatar_service.list_avatar_options), so resolving by id and passing
    # their names through to ItemUpdate.avatars is safe -- item_service
    # re-resolves by name and silently drops anything that no longer exists.
    selected_avatar_names = [a.name for a in avatar_service.resolve_avatars_by_ids(db, avatars)]

    # Only the 8 sidebar fields are user-editable here; everything else
    # (price/dates/status/license/favorite) carries over unchanged from the
    # current record -- still visible in the read-only detail view and
    # editable in bulk from the item list.
    try:
        item_data = ItemUpdate(
            name=name,
            category=ItemCategory(category),
            shop_name=shop_name,
            shop_url=current.shop_url,
            product_url=product_url or None,
            download_source_url=current.download_source_url,
            purchase_date=current.purchase_date,
            download_date=current.download_date,
            price=current.price,
            status_code=current.status_code,
            description=description or None,
            memo=memo or None,
            is_favorite=current.is_favorite,
            tags=_split_csv(tags),
            avatars=selected_avatar_names,
            commercial_use=current.commercial_use,
            modification_allowed=current.modification_allowed,
            redistribution_allowed=current.redistribution_allowed,
            credit_required=current.credit_required,
            license_note=current.license_note,
        )
    except (ValidationError, ValueError) as exc:
        logger.warning("item auto-save validation failed: %s", exc)
        return _status("入力内容を確認してください。", error=True)

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
                return _status(str(exc), error=True)

        try:
            await run_in_threadpool(
                item_service.update_item, db, item_id, item_data, thumbnail_upload=thumbnail_upload
            )
        except NotFoundError:
            raise HTTPException(status_code=404, detail="商品が見つかりません。")
    finally:
        upload_service.cleanup_upload(thumbnail_upload)

    if thumbnail_upload is not None:
        # Fire-and-forget: push the newly-cached thumbnail to Drive in the
        # background rather than waiting on it here (see upload_sync_service).
        asyncio.create_task(asyncio.to_thread(upload_sync_service.sync_pending_now))

    if is_avatar:
        try:
            avatar_service.set_item_as_avatar(
                db, item_id, name=avatar_name.strip() or name, memo=avatar_memo or None
            )
        except IntegrityError:
            db.rollback()
            return _status("このアバター名は既に使用されています。", error=True)
    else:
        avatar_service.unset_item_as_avatar(db, item_id)

    thumbnail_url = f"/items/{item_id}/thumbnail?v={uuid4().hex}" if thumbnail_upload is not None else None
    response = _status("保存しました", error=False, item_name=name, thumbnail_url=thumbnail_url)
    # Background-refreshes the item list (#item-results) so a renamed/
    # retagged item is reflected there without disturbing the still-open
    # edit panel -- see list.html's "item-saved" listener.
    response.headers["HX-Trigger"] = "item-saved"
    return response


@router.post("/{item_id}/merge-into/{target_item_id}", dependencies=[Depends(verify_csrf)])
def merge_item_into_fragment(
    request: Request, item_id: int, target_item_id: int, db: Session = Depends(get_db)
):
    """Resolves the items/_duplicate_confirm.html "統合する" action: folds
    item_id's file(s) onto target_item_id and drops item_id, then swaps the
    whole detail panel over to the merged (target) item -- unlike the
    auto-save route above, this one intentionally replaces the edit panel
    entirely, since the item being edited no longer exists afterward."""
    try:
        item_service.merge_item_into(db, item_id, target_item_id)
        detail = item_service.get_item_detail(db, target_item_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="商品が見つかりません。")
    response = templates.TemplateResponse(request, "items/_detail_panel.html", {"item": detail})
    response.headers["HX-Trigger"] = "item-saved"
    return response


@router.post("/{item_id}/merge-duplicate-into/{target_item_id}", dependencies=[Depends(verify_csrf)])
def merge_duplicate_filename_fragment(item_id: int, target_item_id: int, db: Session = Depends(get_db)):
    """Resolves the upload flow's duplicate-confirm toast (items/list.html:
    showDuplicateConfirmToast). Unlike merge_item_into above -- used for the
    BoothURL-duplicate flow, where the two files may genuinely be worth
    keeping both of -- a filename match means item_id's file is presumed a
    redundant copy of target_item_id's, so it's deleted outright rather than
    kept as an attachment (see item_service.merge_duplicate_group). Called
    via plain fetch rather than htmx, so a bare JSON body is enough."""
    try:
        item_service.merge_duplicate_group(db, [target_item_id, item_id])
    except NotFoundError:
        raise HTTPException(status_code=404, detail="商品が見つかりません。")
    return {"target_item_id": target_item_id}


_INLINE_EDIT_FIELDS = {"tags", "avatars"}


@router.get("/{item_id}/inline-edit/{field}")
def inline_edit_cell_fragment(request: Request, item_id: int, field: str, db: Session = Depends(get_db)):
    """Table view's タグ/対応アバター cells (see items/_results_table.html)
    double-click into this -- a compact combobox scoped to just that one
    cell, saved on blur via submit_inline_edit_cell_fragment below."""
    if field not in _INLINE_EDIT_FIELDS:
        raise HTTPException(status_code=404)
    try:
        detail = item_service.get_item_detail(db, item_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="商品が見つかりません。")

    current_values = detail.tags if field == "tags" else detail.avatars
    options = (
        tag_service.list_tag_names(db)
        if field == "tags"
        else [a.name for a in avatar_service.list_avatar_options(db)]
    )
    return templates.TemplateResponse(
        request,
        "items/_inline_edit_cell.html",
        {"item_id": item_id, "field": field, "options": options, "value": ", ".join(current_values)},
    )


@router.post("/{item_id}/inline-edit/{field}", dependencies=[Depends(verify_csrf)])
def submit_inline_edit_cell_fragment(
    request: Request, item_id: int, field: str, value: str = Form(""), db: Session = Depends(get_db)
):
    if field not in _INLINE_EDIT_FIELDS:
        raise HTTPException(status_code=404)
    try:
        current = item_service.get_item_detail(db, item_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="商品が見つかりません。")

    # Only this one field is user-editable here; everything else carries
    # over unchanged, same approach as the sidebar auto-save route above.
    item_data = ItemUpdate(
        name=current.name,
        category=current.category,
        shop_name=current.shop_name or "未設定",
        shop_url=current.shop_url,
        product_url=current.product_url,
        download_source_url=current.download_source_url,
        purchase_date=current.purchase_date,
        download_date=current.download_date,
        price=current.price,
        status_code=current.status_code,
        description=current.description,
        memo=current.memo,
        is_favorite=current.is_favorite,
        tags=_split_csv(value) if field == "tags" else current.tags,
        avatars=_split_csv(value) if field == "avatars" else current.avatars,
        commercial_use=current.commercial_use,
        modification_allowed=current.modification_allowed,
        redistribution_allowed=current.redistribution_allowed,
        credit_required=current.credit_required,
        license_note=current.license_note,
    )
    updated = item_service.update_item(db, item_id, item_data)
    values = updated.tags if field == "tags" else updated.avatars
    return templates.TemplateResponse(
        request, "items/_inline_display_cell.html", {"item_id": item_id, "field": field, "values": values}
    )
