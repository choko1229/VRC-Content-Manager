"""Download/export: streams an item's primary asset back as a normal
browser download. This is the one place client-facing binary content flows
through the app, so it lives under api/routers per the API/UI split -- pages
and fragments only ever link to it, never render it directly.

Serves from the local cache (file_content_service) whenever possible --
either the pending-upload cache (not yet pushed to Drive) or the 7-day
download cache -- so repeat downloads don't re-fetch from Drive.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.exceptions import DriveError
from app.db.session import get_db
from app.models.item import Item
from app.models.item_file import ItemFile
from app.services import file_content_service, oauth_service

router = APIRouter(prefix="/api/v1")
logger = logging.getLogger(__name__)


def _serve_file(db: Session, item_id: int, file: ItemFile):
    drive_client = None
    if file.synced_at is not None:
        try:
            drive_client = oauth_service.make_drive_client(db)
        except oauth_service.NotConnectedError as exc:
            raise HTTPException(status_code=503, detail="Google Driveが未接続です。") from exc

    try:
        path = file_content_service.resolve_local_path(file, drive_client)
    except DriveError as exc:
        logger.error("download failed for item id=%s: %s", item_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="ダウンロードに失敗しました。") from exc

    logger.info("item id=%s downloaded (item_file id=%s)", item_id, file.id)
    return FileResponse(
        path=path,
        filename=file.original_filename,
        media_type=file.content_type or "application/octet-stream",
    )


@router.get("/items/{item_id}/download")
def download_item_file(item_id: int, db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="商品が見つかりません。")

    primary = item.primary_file
    if primary is None:
        raise HTTPException(status_code=404, detail="ダウンロード可能なファイルがありません。")

    return _serve_file(db, item_id, primary)


@router.get("/items/{item_id}/files/{file_id}/download")
def download_item_attachment_file(item_id: int, file_id: int, db: Session = Depends(get_db)):
    """Downloads one file from an item that has more than one -- e.g. a
    duplicate-BoothURL upload folded in as an ATTACHMENT via
    item_service.merge_item_into rather than replacing the primary file."""
    file = db.get(ItemFile, file_id)
    if file is None or file.item_id != item_id:
        raise HTTPException(status_code=404, detail="ファイルが見つかりません。")

    return _serve_file(db, item_id, file)
