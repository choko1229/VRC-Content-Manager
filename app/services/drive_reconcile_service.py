"""Two-way reconciliation between what's actually on Drive and the DB.

Google Drive is treated as the source of truth for *which files exist*:

- A DB file reference whose Drive object no longer resolves (deleted, or
  moved outside Drive entirely) is dropped -- the item itself is kept
  (metadata/tags/license survive), just the stale file reference goes.
- A file sitting in `upload/` (dropped there by hand) or directly in `file/`
  with no matching DB record is imported as a new, minimally-populated item
  (same philosophy as the quick-upload flow: name from the filename, shop
  "未設定", details filled in later from the sidebar). A file with no DB
  record can end up in `file/` itself if a web upload's Drive write
  succeeded but the follow-up DB write failed and the compensating Drive
  delete also failed (see the "MANUAL CLEANUP NEEDED" log in
  item_service.create_item_with_file) -- scanning `file/` too means that
  failure mode self-heals into a visible item instead of a Drive file the
  app silently ignores forever. Files are grouped by filename stem so a
  same-stem image (e.g. `Item.zip` + `Item.png`) becomes that item's
  thumbnail; a lone image with no sibling asset file has nothing to attach
  to and is left alone. Files picked up from `upload/` are moved into
  `file/`; files already in `file/` are attached in place.
- Any DB-tracked file whose recorded folder isn't the current `file/`
  folder (i.e. it still lives under the old per-avatar/shop nested layout)
  is moved into `file/` and its recorded folder updated -- this is what
  migrates pre-restructure items into the new flat layout.

Root/upload/file folders are resolved via get_or_create_folder, which
transparently recreates them if deleted directly in Drive -- no
special-cased "was it deleted?" branch needed here.

Each item/file processed by the import, migrate, and broken-reference sweeps
is committed (or rolled back) independently rather than batching a whole
sweep into one commit -- one bad file (an unexpected exception, a Drive call
that fails in a new way) can then never silently discard every other file's
already-successful work from the same run, and a partial DB write is never
left half-applied. Newly-synced references are also exempt from the
broken-reference check for a grace period (see _BROKEN_REFERENCE_GRACE_PERIOD)
so a file imported moments ago can't be immediately re-verified and dropped
by a transient Drive hiccup (a concurrent OAuth token refresh, a brief
network error) before it's ever had a real chance to resolve -- losing the
DB's only reference to it while the file itself is still sitting untouched
on Drive.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import DriveError
from app.core.validation import DEFAULT_ALLOWED_EXTENSIONS
from app.db.session import db_write_lock, get_sessionmaker
from app.drive import folder_layout
from app.drive.client import DriveClient
from app.drive.types import FOLDER_MIME_TYPE, DriveFile
from app.models.item import Item
from app.models.item_file import FileRole, ItemFile
from app.models.license import License
from app.models.status import Status
from app.services import drive_sync_service, oauth_service, shop_service

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})
_UNASSIGNED_SHOP_NAME = "未設定"

# Reconcile lists Drive folder contents and Drive-checks every synced file
# each run, so it's pricier than the upload/download sweeps -- a few minutes
# is plenty to make a file dropped directly into Drive's upload/ folder show
# up automatically, without needing the manual "reconcile now" button on
# /settings for every single one.
_RECONCILE_INTERVAL_SECONDS = 300

# A reference is only ever treated as "broken" (and its file dropped) once
# it's been synced for at least this long. Protects a file imported earlier
# in this exact reconcile() call (or by a very recent previous run) from
# being immediately re-verified and wrongly deleted by a one-off Drive
# hiccup -- see the module docstring.
_BROKEN_REFERENCE_GRACE_PERIOD = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    removed_broken_files: int
    imported_items: int
    migrated_files: int


def _extension_of(name: str) -> str:
    idx = name.lower().rfind(".")
    return name.lower()[idx:] if idx != -1 else ""


def _stem_of(name: str) -> str:
    idx = name.rfind(".")
    return (name[:idx] if idx != -1 else name).strip()


def _derive_name(filename: str) -> str:
    stem = _stem_of(filename)
    return (stem or filename.strip() or "無題の商品")[:255]


def _default_status(db: Session) -> Status | None:
    return db.execute(select(Status).where(Status.is_default.is_(True))).scalar_one_or_none()


def reconcile(db: Session, drive_client: DriveClient) -> ReconcileResult:
    # Held for the whole sweep (not just around individual commits): this
    # runs on its own thread and can otherwise overlap with the other
    # background DB-writing flows (upload_sync_service, drive_sync_service,
    # item_service's dedup sweep) closely enough to lose SQLite's
    # busy_timeout race -- see db_write_lock's docstring.
    with db_write_lock:
        file_folder_id = folder_layout.ensure_file_folder(drive_client)
        upload_folder_id = folder_layout.ensure_upload_folder(drive_client)

        migrated = _migrate_legacy_files(db, drive_client, file_folder_id)
        imported = _import_unknown_files(db, drive_client, upload_folder_id, file_folder_id)
        imported += _import_unknown_files(db, drive_client, file_folder_id, file_folder_id)
        removed = _remove_broken_references(db, drive_client)

    if removed or imported or migrated:
        drive_sync_service.mark_dirty()
    logger.info(
        "Drive reconcile complete: removed_broken_files=%d imported_items=%d migrated_files=%d",
        removed,
        imported,
        migrated,
    )
    return ReconcileResult(removed_broken_files=removed, imported_items=imported, migrated_files=migrated)


def _remove_broken_references(db: Session, drive_client: DriveClient) -> int:
    # Files still pending upload_sync_service (synced_at IS NULL, so no
    # drive_file_id yet either) haven't reached Drive yet by design -- that's
    # not brokenness, just skip them. Files synced more recently than the
    # grace period are also skipped -- see _BROKEN_REFERENCE_GRACE_PERIOD.
    cutoff = datetime.now(timezone.utc) - _BROKEN_REFERENCE_GRACE_PERIOD
    files = (
        db.execute(select(ItemFile).where(ItemFile.synced_at.is_not(None), ItemFile.synced_at < cutoff))
        .scalars()
        .all()
    )
    removed = 0
    for file in files:
        try:
            drive_client.get_metadata(file.drive_file_id)
        except DriveError:
            logger.warning(
                "Drive reconcile: dropping stale reference item_id=%s drive_file_id=%s (%s) -- not found on Drive",
                file.item_id,
                file.drive_file_id,
                file.original_filename,
            )
            db.delete(file)
            db.commit()
            removed += 1
    return removed


def _migrate_legacy_files(db: Session, drive_client: DriveClient, file_folder_id: str) -> int:
    """Move any DB-tracked file that isn't already in the flat `file/` folder into it.

    Covers both the pre-restructure per-avatar/shop nested layout and any
    file left behind in `upload/` from an earlier, interrupted import. Files
    still pending upload_sync_service (synced_at IS NULL) have no Drive
    folder yet and are excluded -- there's nothing to move.
    """
    files = db.execute(
        select(ItemFile).where(ItemFile.synced_at.is_not(None), ItemFile.drive_folder_id != file_folder_id)
    ).scalars().all()
    migrated = 0
    for file in files:
        try:
            drive_client.move_file(
                file_id=file.drive_file_id, new_parent_id=file_folder_id, old_parent_id=file.drive_folder_id
            )
        except DriveError:
            logger.warning(
                "Drive reconcile: failed to migrate file id=%s into file/ folder (non-fatal, will retry next run)",
                file.drive_file_id,
                exc_info=True,
            )
            continue
        file.drive_folder_id = file_folder_id
        db.commit()
        migrated += 1
    return migrated


def _import_unknown_files(
    db: Session, drive_client: DriveClient, source_folder_id: str, file_folder_id: str
) -> int:
    """Import files sitting in `source_folder_id` with no matching DB record.

    Called once for `upload/` (manual Drive drops) and once for `file/`
    itself (orphans left behind by a failed post-upload DB write, see the
    module docstring). When source_folder_id == file_folder_id the file is
    already in place and no move is needed.
    """
    known_ids = {row[0] for row in db.execute(select(ItemFile.drive_file_id)).all()}
    entries = [f for f in drive_client.list_folder(source_folder_id) if f.mime_type != FOLDER_MIME_TYPE]
    new_files = [f for f in entries if f.id not in known_ids]
    if not new_files:
        return 0

    groups: dict[str, list[DriveFile]] = {}
    for f in new_files:
        groups.setdefault(_stem_of(f.name), []).append(f)

    status = _default_status(db)
    shop = shop_service.get_or_create_shop(db, name=_UNASSIGNED_SHOP_NAME, url=None)
    imported = 0

    for group in groups.values():
        primaries = [
            f
            for f in group
            if _extension_of(f.name) in DEFAULT_ALLOWED_EXTENSIONS and _extension_of(f.name) not in _IMAGE_EXTENSIONS
        ]
        thumbnail = next((f for f in group if _extension_of(f.name) in _IMAGE_EXTENSIONS), None)

        if not primaries:
            if thumbnail is not None:
                logger.info(
                    "Drive reconcile: skipping orphan image '%s' (no asset file to attach it to)",
                    thumbnail.name,
                )
            continue

        for primary in primaries:
            try:
                item = Item(
                    shop=shop,
                    name=_derive_name(primary.name),
                    file_format=_extension_of(primary.name).lstrip("."),
                    status=status,
                )
                db.add(item)
                db.flush()

                _move_and_attach(
                    db, drive_client, item.id, FileRole.PRIMARY, primary, source_folder_id, file_folder_id
                )
                if thumbnail is not None:
                    _move_and_attach(
                        db, drive_client, item.id, FileRole.THUMBNAIL, thumbnail, source_folder_id, file_folder_id
                    )
                db.add(License(item_id=item.id))
                db.commit()
            except Exception:
                # One poisoned file (an unexpected error, not just a Drive
                # hiccup already handled inside _move_and_attach) must not
                # discard every other file already imported successfully in
                # this same sweep -- roll back just this item and keep going;
                # it'll be retried as a fresh "unknown" file next run.
                db.rollback()
                logger.exception(
                    "Drive reconcile: failed to import '%s' as a new item (skipped, will retry next run)",
                    primary.name,
                )
                continue

            imported += 1
            logger.info(
                "Drive reconcile: imported new item id=%s name=%r from Drive file '%s'",
                item.id,
                item.name,
                primary.name,
            )

    return imported


def _move_and_attach(
    db: Session,
    drive_client: DriveClient,
    item_id: int,
    role: FileRole,
    drive_file: DriveFile,
    old_parent_id: str,
    new_parent_id: str,
) -> None:
    folder_id = old_parent_id
    if old_parent_id != new_parent_id:
        try:
            drive_client.move_file(file_id=drive_file.id, new_parent_id=new_parent_id, old_parent_id=old_parent_id)
            folder_id = new_parent_id
        except DriveError:
            logger.warning(
                "Drive reconcile: failed to move '%s' into file/ folder; leaving reference pointed at its "
                "current folder (non-fatal)",
                drive_file.name,
                exc_info=True,
            )
    db.add(_build_item_file(item_id, role, drive_file, folder_id))


def _build_item_file(item_id: int, role: FileRole, drive_file: DriveFile, folder_id: str) -> ItemFile:
    # Discovered directly on Drive via list_folder, so it's already synced
    # by definition -- there's no local pending-upload cache copy to speak of.
    return ItemFile(
        item_id=item_id,
        file_role=role,
        drive_file_id=drive_file.id,
        drive_folder_id=folder_id,
        original_filename=drive_file.name,
        stored_filename=f"drive:{drive_file.id}",
        content_type=drive_file.mime_type or None,
        size_bytes=drive_file.size_bytes or 0,
        synced_at=datetime.now(timezone.utc),
    )


def _reconcile_now_blocking() -> None:
    session_local = get_sessionmaker()
    with session_local() as db:
        try:
            drive_client = oauth_service.make_drive_client(db)
        except oauth_service.NotConnectedError:
            logger.info("drive_reconcile_service: Drive not connected, skipping periodic reconcile")
            return
        reconcile(db, drive_client)


async def reconcile_loop(stop_event: asyncio.Event) -> None:
    """Periodic fallback so a file dropped directly into Drive's upload/
    folder (or file/ folder) is picked up automatically, the same way a
    file uploaded through the app is -- without requiring a manual visit to
    /settings and clicking "reconcile now" every time."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_RECONCILE_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        try:
            await asyncio.to_thread(_reconcile_now_blocking)
        except Exception:
            logger.exception("drive_reconcile_service: periodic reconcile sweep failed")
