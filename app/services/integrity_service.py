"""Orphan/integrity check: DB-recorded files whose Drive object no longer resolves.

This is the manual-inspection counterpart to the loud "MANUAL CLEANUP NEEDED"
logs item_service emits when a DB write fails after a successful Drive
upload (see app/services/item_service.py) -- run this occasionally (from the
Settings page) to catch anything those logs might have been missed, or Drive
files removed by hand outside the app.

It does not attempt to find the reverse case (Drive files with no DB record)
-- that needs a full recursive folder listing, which DriveClient doesn't
expose yet; this only validates the DB -> Drive direction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import DriveError
from app.drive.client import DriveClient
from app.models.item_file import ItemFile

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BrokenFileReference:
    item_id: int
    item_file_id: int
    file_role: str
    drive_file_id: str
    original_filename: str


def check_for_broken_references(db: Session, drive_client: DriveClient) -> list[BrokenFileReference]:
    broken: list[BrokenFileReference] = []
    files = db.execute(select(ItemFile)).scalars().all()
    for file in files:
        try:
            drive_client.get_metadata(file.drive_file_id)
        except DriveError:
            logger.warning(
                "broken Drive reference: item_id=%s item_file_id=%s drive_file_id=%s (%s)",
                file.item_id,
                file.id,
                file.drive_file_id,
                file.original_filename,
            )
            broken.append(
                BrokenFileReference(
                    item_id=file.item_id,
                    item_file_id=file.id,
                    file_role=file.file_role.value,
                    drive_file_id=file.drive_file_id,
                    original_filename=file.original_filename,
                )
            )
    return broken
