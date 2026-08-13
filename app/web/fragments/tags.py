from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.core.csrf import verify_csrf
from app.core.exceptions import NotFoundError, ValidationError
from app.db.session import get_db
from app.services import tag_service
from app.web.templating import templates

router = APIRouter(prefix="/fragments/tags", dependencies=[Depends(verify_csrf)])
logger = logging.getLogger(__name__)


def _table_response(request: Request, db: Session, error: str | None = None):
    # Always 200, even for a validation/not-found error: htmx 2.x's default
    # responseHandling never swaps a 4xx/5xx response into the DOM (see
    # app/static/js/htmx.min.js's bundled config), so an error status here
    # would silently discard the message this renders instead of showing it
    # -- matches the pattern already used by items.py's refresh-from-booth /
    # fetch-info fragments, which never override the status code either.
    tags = tag_service.list_tags(db)
    return templates.TemplateResponse(
        request,
        "partials/tag_table.html",
        {"tags": tags, "error": error},
    )


@router.post("/{tag_id}/rename")
def rename_tag_fragment(request: Request, tag_id: int, name: str = Form(...), db: Session = Depends(get_db)):
    try:
        tag_service.rename_or_merge_tag(db, tag_id, name)
    except NotFoundError:
        return _table_response(request, db, error="対象のタグが見つかりません。")
    except ValidationError as exc:
        return _table_response(request, db, error=str(exc))

    return _table_response(request, db)


@router.delete("/{tag_id}")
def delete_tag_fragment(request: Request, tag_id: int, db: Session = Depends(get_db)):
    try:
        tag_service.delete_tag(db, tag_id)
    except NotFoundError:
        return _table_response(request, db, error="対象のタグが見つかりません。")

    return _table_response(request, db)
