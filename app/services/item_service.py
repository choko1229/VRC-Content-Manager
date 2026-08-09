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

import asyncio
import logging
import mimetypes
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.core.exceptions import NotFoundError
from app.db.session import db_write_lock, get_sessionmaker
from app.drive.client import DriveClient
from app.models.avatar import Avatar
from app.models.item import Item, ItemCategory
from app.models.item_file import FileRole, ItemFile
from app.models.license import License, TriState
from app.models.status import Status
from app.models.tag import Tag
from app.models.update_history import UpdateHistory
from app.schemas.item import (
    ItemCreate,
    ItemDetail,
    ItemFileRead,
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

# Display labels for ItemCategory -- independent of whether an item is
# registered as a selectable base avatar (Item.as_avatar/avatar_service).
CATEGORY_LABELS: dict[ItemCategory, str] = {
    ItemCategory.CLOTHING: "衣装・アバター素材",
    ItemCategory.AVATAR: "アバター本体",
    ItemCategory.TOOL: "ツール",
    ItemCategory.MA_EXTENSION: "MA拡張",
    ItemCategory.SHADER_EXTENSION: "シェーダー拡張",
    ItemCategory.OTHER: "その他",
}
CATEGORY_OPTIONS: list[tuple[str, str]] = [(c.value, label) for c, label in CATEGORY_LABELS.items()]


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
        category=item.category,
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
        category=item.category,
        category_label=CATEGORY_LABELS[item.category],
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
        primary_file_name=item.primary_file.original_filename if item.primary_file else None,
        attachment_files=[
            ItemFileRead(id=f.id, original_filename=f.original_filename, size_bytes=f.size_bytes)
            for f in item.attachment_files
        ],
    )


def _to_detail(item: Item) -> ItemDetail:
    license_ = item.license
    history = sorted(item.update_history, key=lambda h: h.checked_at, reverse=True)
    return ItemDetail(
        id=item.id,
        name=item.name,
        category=item.category,
        category_label=CATEGORY_LABELS[item.category],
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
        primary_file_name=item.primary_file.original_filename if item.primary_file else None,
        attachment_files=[
            ItemFileRead(id=f.id, original_filename=f.original_filename, size_bytes=f.size_bytes)
            for f in item.attachment_files
        ],
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
    if filters.category:
        stmt = stmt.where(Item.category == filters.category)
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

    # Resolve an auto-fetched thumbnail (thumbnail_service.try_fetch_thumbnail
    # -- a BOOTH network call, robots.txt check included) *before* taking
    # db_write_lock below, same reasoning as create_item_with_file's
    # _resolve_thumbnail: every autosave on every item edit takes that same
    # lock, so a network round-trip held under it would stall every other
    # save in the app for however long BOOTH takes to respond. A caller-
    # supplied thumbnail_upload is already local and doesn't need this.
    if thumbnail_upload is not None:
        resolved_thumbnail: _ResolvedThumbnail | None = _ResolvedThumbnail(upload=thumbnail_upload, owned=False)
    elif item.thumbnail_file is None and data.product_url:
        fetched = thumbnail_service.try_fetch_thumbnail(data.product_url)
        resolved_thumbnail = (
            _ResolvedThumbnail(upload=_write_fetched_thumbnail(fetched), owned=True) if fetched is not None else None
        )
    else:
        resolved_thumbnail = None

    # Held for the whole edit (not just around the commit): a request-path
    # write runs on its own threadpool thread just like the background
    # DB-writing flows, and can otherwise overlap with them (or with another
    # concurrent request) closely enough to lose SQLite's busy_timeout race
    # -- see db_write_lock's docstring.
    with db_write_lock:
        # populate_existing: force a fresh read instead of the stale copy
        # already in this session's identity map from the unlocked read
        # above -- the item could have been deleted (or, harmlessly,
        # further edited) while the thumbnail fetch was in flight.
        item = db.get(Item, item_id, populate_existing=True)
        if item is None:
            raise NotFoundError("Item", item_id)

        shop = shop_service.get_or_create_shop(db, name=data.shop_name, url=data.shop_url)
        tags = tag_service.get_or_create_tags(db, data.tags)
        avatars = avatar_service.resolve_existing_avatars(db, data.avatars)
        status = _resolve_status(db, data.status_code)

        item.shop = shop
        item.name = data.name
        item.category = data.category
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

        if resolved_thumbnail is not None and resolved_thumbnail.owned and item.thumbnail_file is not None:
            # Lost the race: the item picked up a thumbnail some other way
            # while our auto-fetch above was in flight (unlocked). Keep that
            # one and discard ours instead of overwriting it.
            resolved_thumbnail.upload.path.unlink(missing_ok=True)
        elif resolved_thumbnail is not None:
            _apply_thumbnail(db, item, resolved_thumbnail, drive_client)
            db.refresh(item)

    drive_sync_service.mark_dirty()
    logger.info("item updated id=%s", item.id)
    return _to_read(item)


def find_duplicate_product_url_item(db: Session, product_url: str, *, exclude_item_id: int) -> Item | None:
    """The other item (if any) already linked to this exact BOOTH URL --
    used to offer a merge instead of silently letting two items point at
    the same product (see merge_item_into and the edit panel's confirm
    prompt)."""
    return db.execute(
        select(Item).where(Item.product_url == product_url, Item.id != exclude_item_id)
    ).scalars().first()


def merge_item_into(db: Session, source_item_id: int, target_item_id: int) -> ItemRead:
    """Moves every file from `source_item_id` onto `target_item_id` (as
    ATTACHMENT files -- target keeps its own PRIMARY/THUMBNAIL) and deletes
    the now-empty source item. Used when two separately-uploaded files
    turn out to link to the same BOOTH product: rather than leaving two
    library entries for one purchase, the newer upload's file(s) join the
    existing entry instead.

    Best-effort on Drive content, same as delete_item: a synced source file
    is re-parented in place (still the same Drive object, just a different
    DB owner) so nothing needs to be re-uploaded; only a redundant second
    thumbnail is actually deleted.
    """
    # See db_write_lock's docstring -- RLock, so this is safe even when
    # called from auto_merge_duplicate_products, which already holds it.
    with db_write_lock:
        source = db.get(Item, source_item_id)
        target = db.get(Item, target_item_id)
        if source is None:
            raise NotFoundError("Item", source_item_id)
        if target is None:
            raise NotFoundError("Item", target_item_id)

        for file in list(source.files):
            if file.file_role == FileRole.THUMBNAIL:
                if target.thumbnail_file is not None:
                    _delete_file_content(db, file, None)
                    source.files.remove(file)
                    db.delete(file)
                    continue
            else:
                file.file_role = FileRole.ATTACHMENT
            # Re-parenting must go through the relationship collections, not
            # just file.item_id -- source.files has cascade="all, delete-orphan"
            # (see Item.files), which cascades from the *collection membership*
            # at flush time, not the raw foreign key value. Setting item_id
            # alone leaves `file` still logically inside source.files, so the
            # db.delete(source) below would delete it right along with source
            # regardless of what item_id says.
            source.files.remove(file)
            target.files.append(file)

        db.delete(source)
        db.commit()
        db.refresh(target)
    drive_sync_service.mark_dirty()
    logger.info("merged item id=%s into id=%s", source_item_id, target_item_id)
    return _to_read(target)


def find_duplicate_filename_item(db: Session, filename: str, *, exclude_item_id: int) -> Item | None:
    """The other item (if any) whose primary file has this exact original
    filename -- used to offer a merge right after an upload instead of
    silently creating a second entry for a file uploaded twice (unlike a
    BoothURL match, a filename match isn't unambiguous enough to auto-merge,
    so this only ever informs a user-confirmed merge)."""
    return db.execute(
        select(Item)
        .join(ItemFile, ItemFile.item_id == Item.id)
        .where(
            ItemFile.file_role == FileRole.PRIMARY,
            ItemFile.original_filename == filename,
            Item.id != exclude_item_id,
        )
    ).scalars().first()


def find_duplicate_filename_groups(db: Session) -> list[list[Item]]:
    """Every set of 2+ items whose primary file shares an identical original
    filename, oldest-first within each group -- surfaced on /settings so a
    user can confirm merging items that were already uploaded twice before
    this filename check existed (or via Drive-side reconcile picking up the
    same dropped file more than once)."""
    duplicate_filenames = db.execute(
        select(ItemFile.original_filename)
        .where(ItemFile.file_role == FileRole.PRIMARY)
        .group_by(ItemFile.original_filename)
        .having(func.count(ItemFile.item_id) > 1)
    ).scalars().all()

    groups = []
    for filename in duplicate_filenames:
        items = db.execute(
            select(Item)
            .join(ItemFile, ItemFile.item_id == Item.id)
            .where(ItemFile.file_role == FileRole.PRIMARY, ItemFile.original_filename == filename)
            .order_by(Item.created_at.asc(), Item.id.asc())
        ).scalars().all()
        groups.append(list(items))
    return groups


def find_items_without_file(db: Session) -> list[Item]:
    """Items with no PRIMARY file at all -- most often a reconcile-imported
    item whose only file reference was later dropped by
    drive_reconcile_service's broken-reference sweep (before its grace
    period existed -- see that module's docstring), leaving metadata with
    nothing left to download. Surfaced on /settings so these can be found
    without hunting through the list by hand; this only detects the gap --
    the original file, if it wasn't actually deleted from Drive, would need
    to be re-dropped into `upload/` for reconcile to pick it up fresh."""
    return db.execute(
        select(Item).where(~Item.files.any(ItemFile.file_role == FileRole.PRIMARY)).order_by(Item.created_at.asc())
    ).scalars().all()


def merge_duplicate_group(db: Session, item_ids: list[int]) -> ItemDetail:
    """Resolves a filename-duplicate group down to a single item: every id
    after the first is deleted outright (its file included, best-effort on
    Drive -- see delete_item), not folded in as an attachment. Unlike
    merge_item_into (used for the BoothURL-duplicate flow, where the two
    items can be genuinely different files worth keeping both of), a
    filename match means the files are presumed to be redundant copies of
    the very same upload, so only one file should remain in the end.
    `item_ids` must already be ordered target-first -- as returned by
    find_duplicate_filename_groups."""
    # See db_write_lock's docstring -- RLock, so nesting into delete_item's
    # own acquire below is safe.
    with db_write_lock:
        target_id, *duplicate_ids = item_ids
        for duplicate_id in duplicate_ids:
            delete_item(db, duplicate_id)
        return get_item_detail(db, target_id)


def auto_merge_duplicate_products(db: Session) -> int:
    """Retroactively merges any items that already share a BOOTH URL (from
    before this app tracked/prevented that, or from Drive-side reconcile
    importing the same product twice) -- called periodically, see
    item_dedup_service.merge_loop. The earliest-created item in each group
    is kept as the target; every other item's files are folded into it.
    Returns the number of items merged away. See merge_loop for the
    periodic background sweep that calls this automatically.
    """
    # Held for the whole sweep: this runs on its own thread (the periodic
    # dedup loop) and can otherwise overlap with the other background
    # DB-writing flows (upload_sync_service, drive_sync_service,
    # drive_reconcile_service) closely enough to lose SQLite's busy_timeout
    # race -- see db_write_lock's docstring.
    with db_write_lock:
        duplicate_urls = db.execute(
            select(Item.product_url)
            .where(Item.product_url.is_not(None))
            .group_by(Item.product_url)
            .having(func.count(Item.id) > 1)
        ).scalars().all()

        merged = 0
        for url in duplicate_urls:
            items = db.execute(
                select(Item).where(Item.product_url == url).order_by(Item.created_at.asc(), Item.id.asc())
            ).scalars().all()
            target, *duplicates = items
            for dup in duplicates:
                merge_item_into(db, dup.id, target.id)
                merged += 1
        return merged


def delete_item(db: Session, item_id: int, *, drive_client: DriveClient | None = None) -> None:
    """Deletes the DB row (cascades to files/license/history) unconditionally.

    File cleanup is best-effort: a Drive-delete failure leaves an orphaned
    file on Drive (recoverable via the settings integrity check / manual
    cleanup) rather than blocking the user from removing the item from
    their library. A file still pending sync has its local cache copy
    removed instead (nothing's reached Drive yet); a synced file also has
    any download-cache copy dropped, so it doesn't linger for its full TTL.
    """
    # See db_write_lock's docstring -- RLock, so this is safe even when
    # called from merge_duplicate_group, which already holds it.
    with db_write_lock:
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

    # See db_write_lock's docstring.
    with db_write_lock:
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
    # See db_write_lock's docstring.
    with db_write_lock:
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
    # Held from here to the end (not just around the commit): a request-path
    # write runs on its own threadpool thread just like the background
    # DB-writing flows, and uploading several files at once -- the whole
    # point of the TOP page's multi-file drag-and-drop -- fires that many
    # concurrent calls here, each also scheduling its own fire-and-forget
    # upload_sync_service push. That's enough concurrent contention to lose
    # SQLite's busy_timeout race -- see db_write_lock's docstring. Acquired
    # after _resolve_thumbnail's possible BOOTH network fetch above (no DB
    # access, no need to hold the lock through it).
    with db_write_lock:
        try:
            try:
                shop = shop_service.get_or_create_shop(db, name=data.shop_name, url=data.shop_url)
                tags = tag_service.get_or_create_tags(db, data.tags)
                avatars = avatar_service.resolve_existing_avatars(db, data.avatars)
                status = _resolve_status(db, data.status_code)
                item = Item(
                    shop=shop,
                    name=data.name,
                    category=data.category,
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


_MERGE_INTERVAL_SECONDS = 10 * 60


def _auto_merge_now_blocking() -> None:
    session_local = get_sessionmaker()
    with session_local() as db:
        try:
            merged = auto_merge_duplicate_products(db)
            if merged:
                logger.info("periodic dedup sweep merged %d duplicate item(s)", merged)
        except Exception:
            logger.exception("periodic dedup sweep failed")


async def merge_loop(stop_event: asyncio.Event) -> None:
    """Periodic fallback for auto_merge_duplicate_products -- catches
    duplicates the interactive confirm-merge flow (edit panel) didn't
    handle, e.g. two items uploaded before either was linked to BOOTH, or
    ones Drive-side reconcile imported separately."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_MERGE_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        await asyncio.to_thread(_auto_merge_now_blocking)
