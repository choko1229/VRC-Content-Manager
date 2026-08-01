from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow


class FileRole(str, enum.Enum):
    PRIMARY = "primary"
    THUMBNAIL = "thumbnail"
    ATTACHMENT = "attachment"


class ItemFile(Base):
    """A single blob stored on Google Drive and associated with an item.

    Modeled as its own table (rather than drive_file_id columns on `items`)
    so an item can have a primary asset, a thumbnail, and future attachments
    without further schema changes.
    """

    __tablename__ = "item_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    file_role: Mapped[FileRole] = mapped_column(Enum(FileRole, native_enum=False, length=32), nullable=False)
    drive_file_id: Mapped[str | None] = mapped_column(String(128))
    drive_folder_id: Mapped[str | None] = mapped_column(String(128))
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # NULL until the file has been pushed to Drive (see upload_sync_service).
    # While NULL, drive_file_id/drive_folder_id are also NULL and the file's
    # only copy lives in the local pending-upload cache
    # (local_cache_service.pending_upload_path(stored_filename)).
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    item: Mapped["Item"] = relationship(back_populates="files")
