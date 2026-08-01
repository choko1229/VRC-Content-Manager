from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.csrf import verify_csrf
from app.core.exceptions import NotFoundError
from app.db.session import get_db
from app.schemas.item import ItemSearchFilters
from app.services import avatar_service, booth_info_service, item_service, tag_service
from app.web.templating import templates

router = APIRouter(prefix="/fragments/items")
logger = logging.getLogger(__name__)


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


_EMPTY_FETCH_INFO_CONTEXT = {"info": None, "error": None, "suggested_tags": [], "suggested_avatars": []}


@router.get("/fetch-info")
async def fetch_info_fragment(request: Request, product_url: str = "", db: Session = Depends(get_db)):
    product_url = product_url.strip()
    if not product_url:
        return templates.TemplateResponse(request, "items/_fetch_info_result.html", _EMPTY_FETCH_INFO_CONTEXT)

    info = await run_in_threadpool(booth_info_service.try_fetch_product_info, product_url)
    if info is None:
        return templates.TemplateResponse(
            request,
            "items/_fetch_info_result.html",
            {**_EMPTY_FETCH_INFO_CONTEXT, "error": "商品情報を自動取得できませんでした。お手数ですが手動で入力してください。"},
        )

    suggested_tags = booth_info_service.match_known_terms(tag_service.list_tag_names(db), info.name, info.description)
    suggested_avatars = booth_info_service.match_known_terms(
        avatar_service.list_avatar_names(db), info.name, info.description
    )
    return templates.TemplateResponse(
        request,
        "items/_fetch_info_result.html",
        {
            "info": info,
            "error": None,
            "suggested_tags": suggested_tags[:10],
            "suggested_avatars": suggested_avatars[:10],
        },
    )


@router.get("")
def search_items_fragment(request: Request, db: Session = Depends(get_db)):
    params = request.query_params
    filters = ItemSearchFilters(
        keyword=params.get("q") or None,
        tags=_split_csv(params.get("tags", "")),
        avatars=_split_csv(params.get("avatars", "")),
        shop_id=int(params["shop_id"]) if params.get("shop_id") else None,
        status_code=params.get("status_code") or None,
        favorites_only=params.get("favorites_only") == "true",
    )
    view = params.get("view", "card")
    items = item_service.search_items(db, filters)
    template = "items/_results_table.html" if view == "table" else "items/_results_card.html"
    return templates.TemplateResponse(request, template, {"items": items})


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
