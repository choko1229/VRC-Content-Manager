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

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.core.exceptions import DriveError, NotFoundError
from app.drive import folder_layout
from app.drive.client import DriveClient
from app.models.avatar import Avatar
from app.models.item import Item
from app.models.item_file import FileRole, ItemFile
from app.models.license import License, TriState
from app.models.status import Status
from app.models.tag import Tag
from app.models.update_history import UpdateHistory
from app.schemas.item import (
    ItemCreate,
    ItemDetail,
    ItemListRow,
    ItemRead,
    ItemSearchFilters,
    ItemUpdate,
    UpdateHistoryRead,
)
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


def _to_list_row(item: Item) -> ItemListRow:
    return ItemListRow(
        id=item.id,
        name=item.name,
        shop_name=item.shop.name if item.shop else None,
        status_label=item.status.label if item.status else None,
        file_format=item.file_format,
        price=item.price,
        purchase_date=item.purchase_date,
        is_favorite=item.is_favorite,
        has_thumbnail=item.thumbnail_file is not None,
        tags=sorted(t.name for t in item.tags),
        avatars=sorted(a.name for a in item.avatars),
    )


def _to_detail(item: Item) -> ItemDetail:
    license_ = item.license
    history = sorted(item.update_history, key=lambda h: h.checked_at, reverse=True)
    return ItemDetail(
        id=item.id,
        name=item.name,
        shop_id=item.shop_id,
        shop_name=item.shop.name if item.shop else None,
        shop_url=item.shop.url if item.shop else None,
        product_url=item.product_url,
        download_source_url=item.download_source_url,
        purchase_date=item.purchase_date,
        download_date=item.download_date,
        price=item.price,
        file_format=item.file_format,
        status_code=item.status.code if item.status else None,
        status_label=item.status.label if item.status else None,
        description=item.description,
        memo=item.memo,
        is_favorite=item.is_favorite,
        has_thumbnail=item.thumbnail_file is not None,
        tags=sorted(t.name for t in item.tags),
        avatars=sorted(a.name for a in item.avatars),
        avatar_registration_name=item.as_avatar.name if item.as_avatar else None,
        commercial_use=license_.commercial_use if license_ else TriState.UNKNOWN,
        modification_allowed=license_.modification_allowed if license_ else TriState.UNKNOWN,
        redistribution_allowed=license_.redistribution_allowed if license_ else TriState.UNKNOWN,
        credit_required=license_.credit_required if license_ else TriState.UNKNOWN,
        license_note=license_.note if license_ else None,
        update_history=[UpdateHistoryRead(id=h.id, checked_at=h.checked_at, note=h.note) for h in history],
    )


_DETAIL_LOAD_OPTIONS = (
    selectinload(Item.shop),
    selectinload(Item.status),
    selectinload(Item.tags),
    selectinload(Item.avatars),
    selectinload(Item.as_avatar),
    selectinload(Item.files),
    selectinload(Item.license),
    selectinload(Item.update_history),
)


def search_items(db: Session, filters: ItemSearchFilters) -> list[ItemListRow]:
    stmt = select(Item).options(*_DETAIL_LOAD_OPTIONS)

    if filters.keyword:
        like = f"%{filters.keyword}%"
        stmt = stmt.where(or_(Item.name.ilike(like), Item.memo.ilike(like)))
    if filters.shop_id:
        stmt = stmt.where(Item.shop_id == filters.shop_id)
    if filters.status_code:
        stmt = stmt.where(Item.status.has(Status.code == filters.status_code))
    if filters.favorites_only:
        stmt = stmt.where(Item.is_favorite.is_(True))
    if filters.tags:
        stmt = stmt.where(Item.tags.any(Tag.name.in_(filters.tags)))
    if filters.avatars:
        stmt = stmt.where(Item.avatars.any(Avatar.name.in_(filters.avatars)))

    stmt = stmt.order_by(Item.created_at.desc())
    items = db.execute(stmt).unique().scalars().all()
    return [_to_list_row(item) for item in items]


def get_item_detail(db: Session, item_id: int) -> ItemDetail:
    item = db.execute(
        select(Item).options(*_DETAIL_LOAD_OPTIONS).where(Item.id == item_id)
    ).unique().scalar_one_or_none()
    if item is None:
        raise NotFoundError("Item", item_id)
    return _to_detail(item)


def _resolve_edit_thumbnail(item: Item, data: ItemUpdate, thumbnail_upload: ValidatedUpload | None) -> _ResolvedThumbnail | None:
    if thumbnail_upload is not None:
        return _ResolvedThumbnail(upload=thumbnail_upload, owned=False)
    if item.thumbnail_file is not None or not data.product_url:
        return None

    fetched = thumbnail_service.try_fetch_thumbnail(data.product_url)
    if fetched is None:
        return None

    return _ResolvedThumbnail(upload=_write_fetched_thumbnail(fetched), owned=True)


def _apply_thumbnail(db: Session, item: Item, resolved: _ResolvedThumbnail, drive_client: DriveClient | None) -> None:
    """Best-effort: an item's metadata edit must never fail because of the thumbnail
    (matches create_item_with_file's treatment of thumbnails as non-fatal, including
    for Drive-not-connected -- resolving the client happens inside this try too).
    """
    try:
        if drive_client is None:
            drive_client = oauth_service.make_drive_client(db)
        folder_id = folder_layout.ensure_file_folder(drive_client)
        drive_file = drive_client.upload_file(
            local_path=resolved.upload.path,
            name=resolved.upload.original_filename,
            parent_id=folder_id,
            mime_type=resolved.upload.content_type,
        )
        old_thumbnail = item.thumbnail_file
        if old_thumbnail is not None:
            try:
                drive_client.delete_file(old_thumbnail.drive_file_id)
            except Exception:
                logger.warning("failed to delete replaced thumbnail from Drive (non-fatal)", exc_info=True)
            db.delete(old_thumbnail)
            db.flush()
        db.add(
            ItemFile(
                item_id=item.id,
                file_role=FileRole.THUMBNAIL,
                drive_file_id=drive_file.id,
                drive_folder_id=folder_id,
                original_filename=resolved.upload.original_filename,
                stored_filename=resolved.upload.path.name,
                content_type=resolved.upload.content_type,
                size_bytes=resolved.upload.size_bytes,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("thumbnail update failed during item edit (non-fatal)", exc_info=True)
    finally:
        if resolved.owned:
            resolved.upload.path.unlink(missing_ok=True)


def update_item(
    db: Session,
    item_id: int,
    data: ItemUpdate,
    *,
    thumbnail_upload: ValidatedUpload | None = None,
    drive_client: DriveClient | None = None,
) -> ItemRead:
    item = db.get(Item, item_id)
    if item is None:
        raise NotFoundError("Item", item_id)

    shop = shop_service.get_or_create_shop(db, name=data.shop_name, url=data.shop_url)
    tags = tag_service.get_or_create_tags(db, data.tags)
    avatars = avatar_service.resolve_existing_avatars(db, data.avatars)
    status = _resolve_status(db, data.status_code)

    item.shop = shop
    item.name = data.name
    item.product_url = data.product_url
    item.download_source_url = data.download_source_url
    item.purchase_date = data.purchase_date
    item.download_date = data.download_date
    item.price = data.price
    item.status = status
    item.description = data.description
    item.memo = data.memo
    item.is_favorite = data.is_favorite
    item.tags = tags
    item.avatars = avatars

    if item.license is None:
        item.license = License(item_id=item.id)
    item.license.commercial_use = data.commercial_use
    item.license.modification_allowed = data.modification_allowed
    item.license.redistribution_allowed = data.redistribution_allowed
    item.license.credit_required = data.credit_required
    item.license.note = data.license_note

    db.commit()
    db.refresh(item)

    resolved_thumbnail = _resolve_edit_thumbnail(item, data, thumbnail_upload)
    if resolved_thumbnail is not None:
        _apply_thumbnail(db, item, resolved_thumbnail, drive_client)
        db.refresh(item)

    drive_sync_service.mark_dirty()
    logger.info("item updated id=%s", item.id)
    return _to_read(item)


def delete_item(db: Session, item_id: int, *, drive_client: DriveClient | None = None) -> None:
    """Deletes the DB row (cascades to files/license/history) unconditionally.

    Drive file cleanup is best-effort: a failure here leaves an orphaned file
    on Drive (recoverable via the settings integrity check / manual cleanup)
    rather than blocking the user from removing the item from their library.
    """
    item = db.get(Item, item_id)
    if item is None:
        raise NotFoundError("Item", item_id)

    file_ids = [f.drive_file_id for f in item.files]
    if file_ids:
        try:
            if drive_client is None:
                drive_client = oauth_service.make_drive_client(db)
            for file_id in file_ids:
                try:
                    drive_client.delete_file(file_id)
                except Exception:
                    logger.warning(
                        "failed to delete Drive file id=%s for item id=%s (non-fatal)", file_id, item_id, exc_info=True
                    )
        except Exception:
            logger.warning("could not reach Drive to clean up files for item id=%s (non-fatal)", item_id, exc_info=True)

    db.delete(item)
    db.commit()
    drive_sync_service.mark_dirty()
    logger.info("item deleted id=%s", item_id)


def bulk_update(
    db: Session,
    item_ids: list[int],
    *,
    status_code: str | None = None,
    add_tag_names: list[str] | None = None,
    add_avatar_names: list[str] | None = None,
    is_favorite: bool | None = None,
) -> int:
    """Applies the same change to many items at once for bulk curation
    (set status, add tags/avatars, toggle favorite) -- unlike update_item,
    every field here is optional and a left-unset field is left untouched
    rather than required. Tags/avatars are unioned onto each item's
    existing set, never replaced (so "add" can't accidentally remove
    something an item already had that others in the selection don't).
    """
    if not item_ids:
        return 0

    status = _resolve_status(db, status_code) if status_code else None
    add_tags = tag_service.get_or_create_tags(db, add_tag_names) if add_tag_names else []
    add_avatars = avatar_service.resolve_existing_avatars(db, add_avatar_names) if add_avatar_names else []

    items = db.execute(select(Item).where(Item.id.in_(item_ids))).scalars().all()
    for item in items:
        if status is not None:
            item.status = status
        if add_tags:
            existing_tag_ids = {t.id for t in item.tags}
            item.tags = item.tags + [t for t in add_tags if t.id not in existing_tag_ids]
        if add_avatars:
            existing_avatar_ids = {a.id for a in item.avatars}
            item.avatars = item.avatars + [a for a in add_avatars if a.id not in existing_avatar_ids]
        if is_favorite is not None:
            item.is_favorite = is_favorite

    db.commit()
    if items:
        drive_sync_service.mark_dirty()
    logger.info("bulk update applied to %d item(s)", len(items))
    return len(items)


def add_update_check(db: Session, item_id: int, note: str | None) -> None:
    item = db.get(Item, item_id)
    if item is None:
        raise NotFoundError("Item", item_id)
    db.add(UpdateHistory(item_id=item.id, note=note))
    db.commit()
    drive_sync_service.mark_dirty()
    logger.info("update check recorded for item id=%s", item.id)


def _write_fetched_thumbnail(fetched: thumbnail_service.FetchedThumbnail) -> ValidatedUpload:
    settings = get_settings()
    settings.upload_tmp_dir.mkdir(parents=True, exist_ok=True)
    ext = mimetypes.guess_extension(fetched.content_type) or ".jpg"
    tmp_path = settings.upload_tmp_dir / f"{uuid.uuid4().hex}{ext}"
    tmp_path.write_bytes(fetched.content)
    return ValidatedUpload(
        path=tmp_path,
        original_filename=f"thumbnail{ext}",
        size_bytes=len(fetched.content),
        content_type=fetched.content_type,
        extension=ext,
    )


def _resolve_thumbnail(data: ItemCreate, thumbnail_upload: ValidatedUpload | None) -> _ResolvedThumbnail | None:
    if thumbnail_upload is not None:
        return _ResolvedThumbnail(upload=thumbnail_upload, owned=False)
    if not data.product_url:
        return None

    fetched = thumbnail_service.try_fetch_thumbnail(data.product_url)
    if fetched is None:
        return None

    return _ResolvedThumbnail(upload=_write_fetched_thumbnail(fetched), owned=True)


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

    resolved_thumbnail = _resolve_thumbnail(data, thumbnail_upload)
    try:
        # All Drive I/O (folder + uploads) happens before any DB write below.
        # get_or_create_shop/tags each do their own db.flush(), which
        # would otherwise start a write transaction and hold SQLite's
        # single-writer lock open for the entire network round-trip -- with
        # concurrent uploads (e.g. dropping several files at once) that's
        # long enough to blow past the busy_timeout and raise "database is
        # locked" for whichever request is still waiting.
        folder_id = folder_layout.ensure_file_folder(drive_client)

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
            # Nothing has touched the DB yet at this point, so there's
            # nothing to roll back -- just surface the failure.
            raise DriveError(f"failed to upload '{primary_upload.original_filename}' to Drive") from exc

        try:
            shop = shop_service.get_or_create_shop(db, name=data.shop_name, url=data.shop_url)
            tags = tag_service.get_or_create_tags(db, data.tags)
            avatars = avatar_service.resolve_existing_avatars(db, data.avatars)
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
                description=data.description,
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
