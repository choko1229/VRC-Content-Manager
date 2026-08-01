"""Two-way reconciliation between what's actually on Drive and the DB.

Google Drive is treated as the source of truth for *which files exist*:

- A DB file reference whose Drive object no longer resolves (deleted, or
  moved outside the tracked tree) is dropped -- the item itself is kept
  (metadata/tags/license survive), just the stale file reference goes.
- A file sitting directly in an avatar/shop_item leaf folder with no
  matching DB record is imported as a new, minimally-populated item (same
  philosophy as the quick-upload flow: name from the filename, shop
  "未設定", details filled in later on the edit page). One item per
  non-image file; a same-folder image becomes that item's thumbnail. A
  lone image with no sibling asset file has nothing to attach to and is
  left alone.

Only the tree folder_layout itself writes to (root -> avatar -> shop_item
-> files) is walked; the `_db` folder (the SQLite snapshot) is skipped
entirely. The root folder is resolved via get_or_create_folder, which
transparently recreates it if it was deleted directly in Drive -- no
special-cased "was it deleted?" branch needed here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.validation import DEFAULT_ALLOWED_EXTENSIONS
from app.drive import folder_layout
from app.drive.client import DriveClient
from app.drive.types import FOLDER_MIME_TYPE, DriveFile
from app.models.item import Item
from app.models.item_file import FileRole, ItemFile
from app.models.license import License
from app.models.status import Status
from app.services import avatar_service, drive_sync_service, shop_service

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})
_UNASSIGNED_SHOP_NAME = "未設定"


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    removed_broken_files: int
    imported_items: int


def _extension_of(name: str) -> str:
    idx = name.lower().rfind(".")
    return name.lower()[idx:] if idx != -1 else ""


def _derive_name(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0].strip()
    return (stem or filename.strip() or "無題の商品")[:255]


def _default_status(db: Session) -> Status | None:
    return db.execute(select(Status).where(Status.is_default.is_(True))).scalar_one_or_none()


def reconcile(db: Session, drive_client: DriveClient) -> ReconcileResult:
    root_id = folder_layout.ensure_folder_path(drive_client, folder_layout.ROOT_FOLDER_NAME)

    seen_file_ids: set[str] = set()
    leaf_files: dict[tuple[str, str, str], list[DriveFile]] = {}

    for avatar_folder in drive_client.list_folder(root_id):
        if avatar_folder.mime_type != FOLDER_MIME_TYPE or avatar_folder.name == folder_layout.DB_FOLDER_NAME:
            continue
        for item_folder in drive_client.list_folder(avatar_folder.id):
            if item_folder.mime_type != FOLDER_MIME_TYPE:
                continue
            entries = drive_client.list_folder(item_folder.id)
            files = [f for f in entries if f.mime_type != FOLDER_MIME_TYPE]
            for f in files:
                seen_file_ids.add(f.id)
            leaf_files[(avatar_folder.name, item_folder.name, item_folder.id)] = files

    removed = _remove_broken_references(db, seen_file_ids)
    imported = _import_unknown_files(db, leaf_files)

    if removed or imported:
        drive_sync_service.mark_dirty()
    logger.info("Drive reconcile complete: removed_broken_files=%d imported_items=%d", removed, imported)
    return ReconcileResult(removed_broken_files=removed, imported_items=imported)


def _remove_broken_references(db: Session, seen_file_ids: set[str]) -> int:
    files = db.execute(select(ItemFile)).scalars().all()
    removed = 0
    for file in files:
        if file.drive_file_id not in seen_file_ids:
            logger.warning(
                "Drive reconcile: dropping stale reference item_id=%s drive_file_id=%s (%s) -- not found on Drive",
                file.item_id,
                file.drive_file_id,
                file.original_filename,
            )
            db.delete(file)
            removed += 1
    if removed:
        db.commit()
    return removed


def _import_unknown_files(
    db: Session, leaf_files: dict[tuple[str, str, str], list[DriveFile]]
) -> int:
    known_ids = {row[0] for row in db.execute(select(ItemFile.drive_file_id)).all()}
    imported = 0

    for (avatar_folder_name, item_folder_name, item_folder_id), files in leaf_files.items():
        new_files = [f for f in files if f.id not in known_ids]
        if not new_files:
            continue

        primaries = [
            f
            for f in new_files
            if _extension_of(f.name) in DEFAULT_ALLOWED_EXTENSIONS and _extension_of(f.name) not in _IMAGE_EXTENSIONS
        ]
        thumbnail = next((f for f in new_files if _extension_of(f.name) in _IMAGE_EXTENSIONS), None)

        if not primaries:
            if thumbnail is not None:
                logger.info(
                    "Drive reconcile: skipping orphan image '%s' in folder '%s' (no asset file to attach it to)",
                    thumbnail.name,
                    item_folder_name,
                )
            continue

        avatar_name = None if avatar_folder_name == folder_layout.UNASSIGNED_AVATAR_FOLDER_NAME else avatar_folder_name
        shop = shop_service.get_or_create_shop(db, name=_UNASSIGNED_SHOP_NAME, url=None)
        avatars = avatar_service.get_or_create_avatars(db, [avatar_name]) if avatar_name else []

        for primary in primaries:
            item = Item(
                shop=shop,
                name=_derive_name(primary.name),
                file_format=_extension_of(primary.name).lstrip("."),
                status=_default_status(db),
                avatars=avatars,
            )
            db.add(item)
            db.flush()

            db.add(_build_item_file(item.id, FileRole.PRIMARY, primary, item_folder_id))
            if thumbnail is not None:
                db.add(_build_item_file(item.id, FileRole.THUMBNAIL, thumbnail, item_folder_id))
            db.add(License(item_id=item.id))

            imported += 1
            logger.info(
                "Drive reconcile: imported new item id=%s name=%r from Drive file '%s'",
                item.id,
                item.name,
                primary.name,
            )

    if imported:
        db.commit()
    return imported


def _build_item_file(item_id: int, role: FileRole, drive_file: DriveFile, folder_id: str) -> ItemFile:
    return ItemFile(
        item_id=item_id,
        file_role=role,
        drive_file_id=drive_file.id,
        drive_folder_id=folder_id,
        original_filename=drive_file.name,
        stored_filename=f"drive:{drive_file.id}",
        content_type=drive_file.mime_type or None,
        size_bytes=drive_file.size_bytes or 0,
    )
