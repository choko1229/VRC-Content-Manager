from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.core.csrf import verify_csrf
from app.core.exceptions import NotFoundError, ValidationError
from app.db.session import get_db
from app.services import saved_filter_service
from app.web.templating import templates

router = APIRouter(prefix="/fragments/saved-filters", dependencies=[Depends(verify_csrf)])
logger = logging.getLogger(__name__)


def _list_response(request: Request, db: Session, error: str | None = None):
    # Always 200, even for a validation/not-found error: htmx 2.x's default
    # responseHandling never swaps a 4xx/5xx response into the DOM (see
    # app/static/js/htmx.min.js's bundled config), so an error status here
    # would silently discard the message this renders instead of showing it
    # -- matches the pattern already used by items.py's refresh-from-booth /
    # fetch-info fragments, which never override the status code either.
    saved_filters = saved_filter_service.list_saved_filters(db)
    return templates.TemplateResponse(
        request,
        "items/_saved_filter_list.html",
        {"saved_filters": saved_filters, "error": error},
    )


@router.post("")
def create_saved_filter_fragment(
    request: Request, name: str = Form(...), query_string: str = Form(""), db: Session = Depends(get_db)
):
    try:
        saved_filter_service.create_saved_filter(db, name, query_string)
    except ValidationError as exc:
        return _list_response(request, db, error=str(exc))

    return _list_response(request, db)


@router.delete("/{saved_filter_id}")
def delete_saved_filter_fragment(request: Request, saved_filter_id: int, db: Session = Depends(get_db)):
    try:
        saved_filter_service.delete_saved_filter(db, saved_filter_id)
    except NotFoundError:
        return _list_response(request, db, error="対象の保存フィルタが見つかりません。")

    return _list_response(request, db)
