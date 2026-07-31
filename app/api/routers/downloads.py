"""Download/export: streams an item's primary asset back from Drive as a normal
browser download. This is the one place client-facing binary content flows
through the app, so it lives under api/routers per the API/UI split -- pages
and fragments only ever link to it, never render it directly.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.config import get_settings
from app.core.exceptions import DriveError
from app.db.session import get_db
from app.models.item import Item
from app.services import oauth_service

router = APIRouter(prefix="/api/v1")
logger = logging.getLogger(__name__)


@router.get("/items/{item_id}/download")
def download_item_file(item_id: int, db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="商品が見つかりません。")

    primary = item.primary_file
    if primary is None:
        raise HTTPException(status_code=404, detail="ダウンロード可能なファイルがありません。")

    try:
        drive_client = oauth_service.make_drive_client(db)
    except oauth_service.NotConnectedError as exc:
        raise HTTPException(status_code=503, detail="Google Driveが未接続です。") from exc

    settings = get_settings()
    settings.upload_tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = settings.upload_tmp_dir / f"dl_{uuid.uuid4().hex}"
    try:
        drive_client.download_file(file_id=primary.drive_file_id, dest_path=tmp_path)
    except DriveError as exc:
        tmp_path.unlink(missing_ok=True)
        logger.error("download failed for item id=%s: %s", item_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Google Driveからのダウンロードに失敗しました。") from exc

    logger.info("item id=%s downloaded (drive_file_id=%s)", item_id, primary.drive_file_id)
    return FileResponse(
        path=tmp_path,
        filename=primary.original_filename,
        media_type=primary.content_type or "application/octet-stream",
        background=BackgroundTask(tmp_path.unlink, missing_ok=True),
    )
