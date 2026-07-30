"""Item ingest orchestration: validate -> upload to Drive -> write DB rows.

Ordering is the load-bearing part of this module (see create_item_with_file):
Drive upload happens first and nothing is committed to the DB until it
succeeds, so a Drive failure can never leave an orphan DB row. If the DB
write fails *after* a successful Drive upload, we attempt a compensating
delete on Drive; if that also fails, it's logged loudly as a
manual-cleanup-needed case rather than silently swallowed -- that's the one
failure mode here that can't be fully self-healed.
"""

from __future__ import annotations

import logging
import mimetypes
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.exceptions import DriveError
from app.drive import folder_layout
from app.drive.client import DriveClient
from app.models.item import Item
from app.models.item_file import FileRole, ItemFile
from app.models.license import License
from app.models.status import Status
from app.schemas.item import ItemCreate, ItemRead
from app.services import avatar_service, drive_sync_service, oauth_service, shop_service, tag_service, thumbnail_service
from app.services.upload_service import ValidatedUpload

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ResolvedThumbnail:
    upload: ValidatedUpload
    owned: bool  # True if we created the temp file ourselves (auto-fetch) and must clean it up


def _to_read(item: Item) -> ItemRead:
    return ItemRead(
        id=item.id,
        name=item.name,
        shop_name=item.shop.name if item.shop else None,
        status_label=item.status.label if item.status else None,
        file_format=item.file_format,
        is_favorite=item.is_favorite,
        has_thumbnail=item.thumbnail_file is not None,
        tags=sorted(t.name for t in item.tags),
        avatars=sorted(a.name for a in item.avatars),
    )


def _resolve_thumbnail(data: ItemCreate, thumbnail_upload: ValidatedUpload | None) -> _ResolvedThumbnail | None:
    if thumbnail_upload is not None:
        return _ResolvedThumbnail(upload=thumbnail_upload, owned=False)
    if not data.product_url:
        return None

    fetched = thumbnail_service.try_fetch_thumbnail(data.product_url)
    if fetched is None:
        return None

    settings = get_settings()
    settings.upload_tmp_dir.mkdir(parents=True, exist_ok=True)
    ext = mimetypes.guess_extension(fetched.content_type) or ".jpg"
    tmp_path = settings.upload_tmp_dir / f"{uuid.uuid4().hex}{ext}"
    tmp_path.write_bytes(fetched.content)
    upload = ValidatedUpload(
        path=tmp_path,
        original_filename=f"thumbnail{ext}",
        size_bytes=len(fetched.content),
        content_type=fetched.content_type,
        extension=ext,
    )
    return _ResolvedThumbnail(upload=upload, owned=True)


def _resolve_status(db: Session, status_code: str | None) -> Status | None:
    if status_code:
        return db.execute(select(Status).where(Status.code == status_code)).scalar_one_or_none()
    return db.execute(select(Status).where(Status.is_default.is_(True))).scalar_one_or_none()


def create_item_with_file(
    db: Session,
    *,
    data: ItemCreate,
    primary_upload: ValidatedUpload,
    thumbnail_upload: ValidatedUpload | None = None,
    drive_client: DriveClient | None = None,
) -> ItemRead:
    if drive_client is None:
        drive_client = oauth_service.make_drive_client(db)

    shop = shop_service.get_or_create_shop(db, name=data.shop_name, url=data.shop_url)
    tags = tag_service.get_or_create_tags(db, data.tags)
    avatars = avatar_service.get_or_create_avatars(db, data.avatars)

    resolved_thumbnail = _resolve_thumbnail(data, thumbnail_upload)
    try:
        first_avatar_name = avatars[0].name if avatars else None
        folder_id = folder_layout.ensure_item_folder(
            drive_client, avatar_name=first_avatar_name, shop_name=shop.name, item_name=data.name
        )

        uploaded_drive_file_ids: list[str] = []
        try:
            primary_drive_file = drive_client.upload_file(
                local_path=primary_upload.path,
                name=primary_upload.original_filename,
                parent_id=folder_id,
                mime_type=primary_upload.content_type,
            )
            uploaded_drive_file_ids.append(primary_drive_file.id)

            thumbnail_drive_file = None
            if resolved_thumbnail is not None:
                try:
                    thumbnail_drive_file = drive_client.upload_file(
                        local_path=resolved_thumbnail.upload.path,
                        name=resolved_thumbnail.upload.original_filename,
                        parent_id=folder_id,
                        mime_type=resolved_thumbnail.upload.content_type,
                    )
                    uploaded_drive_file_ids.append(thumbnail_drive_file.id)
                except Exception:
                    # Thumbnail is best-effort; never fail the whole ingest for it.
                    logger.warning("thumbnail upload to Drive failed (non-fatal)", exc_info=True)
                    thumbnail_drive_file = None
        except Exception as exc:
            db.rollback()
            raise DriveError(f"failed to upload '{primary_upload.original_filename}' to Drive") from exc

        try:
            status = _resolve_status(db, data.status_code)
            item = Item(
                shop=shop,
                name=data.name,
                product_url=data.product_url,
                download_source_url=data.download_source_url,
                purchase_date=data.purchase_date,
                download_date=data.download_date,
                price=data.price,
                file_format=primary_upload.extension.lstrip("."),
                status=status,
                memo=data.memo,
                is_favorite=data.is_favorite,
                tags=tags,
                avatars=avatars,
            )
            db.add(item)
            db.flush()

            db.add(
                ItemFile(
                    item_id=item.id,
                    file_role=FileRole.PRIMARY,
                    drive_file_id=primary_drive_file.id,
                    drive_folder_id=folder_id,
                    original_filename=primary_upload.original_filename,
                    stored_filename=primary_upload.path.name,
                    content_type=primary_upload.content_type,
                    size_bytes=primary_upload.size_bytes,
                )
            )
            if thumbnail_drive_file is not None and resolved_thumbnail is not None:
                db.add(
                    ItemFile(
                        item_id=item.id,
                        file_role=FileRole.THUMBNAIL,
                        drive_file_id=thumbnail_drive_file.id,
                        drive_folder_id=folder_id,
                        original_filename=resolved_thumbnail.upload.original_filename,
                        stored_filename=resolved_thumbnail.upload.path.name,
                        content_type=resolved_thumbnail.upload.content_type,
                        size_bytes=resolved_thumbnail.upload.size_bytes,
                    )
                )

            db.add(
                License(
                    item_id=item.id,
                    commercial_use=data.commercial_use,
                    modification_allowed=data.modification_allowed,
                    redistribution_allowed=data.redistribution_allowed,
                    credit_required=data.credit_required,
                    note=data.license_note,
                )
            )

            db.commit()
            db.refresh(item)
        except Exception:
            db.rollback()
            logger.error(
                "DB write failed after Drive upload succeeded; attempting compensating delete of %s",
                uploaded_drive_file_ids,
            )
            for file_id in uploaded_drive_file_ids:
                try:
                    drive_client.delete_file(file_id)
                except Exception:
                    logger.error(
                        "MANUAL CLEANUP NEEDED: failed to delete orphaned Drive file id=%s "
                        "after a DB write failure -- it was never recorded in the database",
                        file_id,
                        exc_info=True,
                    )
            raise

        drive_sync_service.mark_dirty()
        logger.info("item created id=%s name=%s", item.id, item.name)
        return _to_read(item)
    finally:
        if resolved_thumbnail is not None and resolved_thumbnail.owned:
            resolved_thumbnail.upload.path.unlink(missing_ok=True)
