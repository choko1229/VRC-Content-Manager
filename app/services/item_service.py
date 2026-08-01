"""Item ingest orchestration: cache the upload locally -> write DB rows ->
sync to Drive in the background.

create_item_with_file makes no Drive call by default: the validated upload
is moved into the local pending-upload cache (local_cache_service) and the
DB row is written immediately, with its ItemFile.synced_at left NULL.
upload_sync_service pushes pending files to Drive shortly after, off the
request path entirely -- this is what keeps uploads fast regardless of
Drive's latency, and lets uploading work even while Drive is unreachable.
A caller that explicitly passes `drive_client` (tests, or anything that
wants synchronous/deterministic behavior) gets the file pushed to Drive
immediately instead of waiting on the background sync.
"""

from __future__ import annotations

import logging
import mimetypes
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.core.exceptions import NotFoundError
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
from app.services import (
    avatar_service,
    drive_sync_service,
    local_cache_service,
    oauth_service,
    shop_service,
    tag_service,
    thumbnail_service,
    upload_sync_service,
)
from app.services.upload_service import ValidatedUpload

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ResolvedThumbnail:
    upload: ValidatedUpload
    owned: bool  # True if we created the temp file ourselves (auto-fetch) and must clean it up


def _file_status(primary: ItemFile | None) -> str | None:
    if primary is None:
        return None
    if primary.synced_at is None:
        return "pending"
    if primary.drive_file_id and local_cache_service.peek_download_cached(primary.drive_file_id):
        return "cached"
    return "synced"


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
        file_status=_file_status(item.primary_file),
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
        file_status=_file_status(item.primary_file),
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


def _delete_file_content(db: Session, item_file: ItemFile, drive_client: DriveClient | None) -> None:
    """Best-effort removal of an ItemFile's underlying blob -- the Drive file
    if it's been synced, otherwise its local pending-upload cache copy.
    Does not touch the DB row; callers delete that themselves."""
    if item_file.synced_at is not None and item_file.drive_file_id:
        try:
            if drive_client is None:
                drive_client = oauth_service.make_drive_client(db)
            drive_client.delete_file(item_file.drive_file_id)
        except Exception:
            logger.warning(
                "failed to delete Drive file id=%s for item_file id=%s (non-fatal)",
                item_file.drive_file_id,
                item_file.id,
                exc_info=True,
            )
    else:
        local_cache_service.pending_upload_path(item_file.stored_filename).unlink(missing_ok=True)


def _apply_thumbnail(db: Session, item: Item, resolved: _ResolvedThumbnail, drive_client: DriveClient | None) -> None:
    """Best-effort: an item's metadata edit must never fail because of the
    thumbnail. Like create_item_with_file, this caches the file locally and
    leaves synced_at NULL for upload_sync_service to push in the background,
    unless the caller explicitly hands us a drive_client for immediate sync.
    """
    try:
        cache_path = _cache_upload_locally(resolved.upload)
        old_thumbnail = item.thumbnail_file
        if old_thumbnail is not None:
            _delete_file_content(db, old_thumbnail, drive_client)
            db.delete(old_thumbnail)
            db.flush()

        new_file = ItemFile(
            item_id=item.id,
            file_role=FileRole.THUMBNAIL,
            original_filename=resolved.upload.original_filename,
            stored_filename=cache_path.name,
            content_type=resolved.upload.content_type,
            size_bytes=resolved.upload.size_bytes,
        )
        db.add(new_file)
        db.commit()
        db.refresh(new_file)

        if drive_client is not None:
            upload_sync_service.sync_item_file(db, new_file.id, drive_client)
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

    File cleanup is best-effort: a Drive-delete failure leaves an orphaned
    file on Drive (recoverable via the settings integrity check / manual
    cleanup) rather than blocking the user from removing the item from
    their library. A file still pending sync has its local cache copy
    removed instead (nothing's reached Drive yet); a synced file also has
    any download-cache copy dropped, so it doesn't linger for its full TTL.
    """
    item = db.get(Item, item_id)
    if item is None:
        raise NotFoundError("Item", item_id)

    for file in item.files:
        _delete_file_content(db, file, drive_client)
        if file.drive_file_id:
            local_cache_service.forget_download(file.drive_file_id)

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


def _cache_upload_locally(upload: ValidatedUpload) -> Path:
    """Moves a validated upload out of the request-scoped tmp dir into the
    persistent pending-upload cache, where it survives until
    upload_sync_service confirms it's been pushed to Drive."""
    dest = local_cache_service.pending_upload_path(upload.path.name)
    upload.path.rename(dest)
    return dest


def create_item_with_file(
    db: Session,
    *,
    data: ItemCreate,
    primary_upload: ValidatedUpload,
    thumbnail_upload: ValidatedUpload | None = None,
    drive_client: DriveClient | None = None,
) -> ItemRead:
    resolved_thumbnail = _resolve_thumbnail(data, thumbnail_upload)
    moved_cache_paths: list[Path] = []
    try:
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

            primary_cache_path = _cache_upload_locally(primary_upload)
            moved_cache_paths.append(primary_cache_path)
            primary_file = ItemFile(
                item_id=item.id,
                file_role=FileRole.PRIMARY,
                original_filename=primary_upload.original_filename,
                stored_filename=primary_cache_path.name,
                content_type=primary_upload.content_type,
                size_bytes=primary_upload.size_bytes,
            )
            db.add(primary_file)

            thumbnail_file = None
            if resolved_thumbnail is not None:
                thumb_cache_path = _cache_upload_locally(resolved_thumbnail.upload)
                moved_cache_paths.append(thumb_cache_path)
                thumbnail_file = ItemFile(
                    item_id=item.id,
                    file_role=FileRole.THUMBNAIL,
                    original_filename=resolved_thumbnail.upload.original_filename,
                    stored_filename=thumb_cache_path.name,
                    content_type=resolved_thumbnail.upload.content_type,
                    size_bytes=resolved_thumbnail.upload.size_bytes,
                )
                db.add(thumbnail_file)

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
            # Nothing committed, so any file already moved into the pending
            # cache would otherwise be orphaned (no DB row will ever point
            # at it).
            for path in moved_cache_paths:
                path.unlink(missing_ok=True)
            raise
    finally:
        # Best-effort BOOTH-fetched thumbnail cleanup: a no-op if it was
        # already moved into the pending-upload cache above, so this only
        # actually deletes anything if caching failed partway through.
        if resolved_thumbnail is not None and resolved_thumbnail.owned:
            resolved_thumbnail.upload.path.unlink(missing_ok=True)

    drive_sync_service.mark_dirty()

    # No Drive call above -- the file(s) just sit in the pending-upload cache
    # until upload_sync_service's background loop pushes them. A caller that
    # explicitly hands us a drive_client (tests, or anything that wants
    # synchronous/deterministic behavior) gets that push done immediately instead.
    if drive_client is not None:
        upload_sync_service.sync_item_file(db, primary_file.id, drive_client)
        if thumbnail_file is not None:
            upload_sync_service.sync_item_file(db, thumbnail_file.id, drive_client)
        db.refresh(item)

    logger.info("item created id=%s name=%s", item.id, item.name)
    return _to_read(item)
