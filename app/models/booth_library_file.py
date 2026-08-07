from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class BoothLibraryFile(Base):
    """One row per (product, filename) pair pulled from the user's own BOOTH
    purchase library (accounts.booth.pm/library) -- see booth_library_service.

    The whole table is replaced wholesale on every sync (delete + reinsert)
    rather than diffed, since the library listing itself is always the
    source of truth and there's no meaningful local state to preserve
    between syncs. filename is indexed since the only read path is an exact
    lookup by an uploaded file's original_filename, used to suggest a
    high-confidence BoothURL match without hitting BOOTH's public search.
    """

    __tablename__ = "booth_library_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    product_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    product_name: Mapped[str] = mapped_column(String(512), nullable=False)
    shop_name: Mapped[str | None] = mapped_column(String(255))
    shop_url: Mapped[str | None] = mapped_column(String(1024))
    thumbnail_url: Mapped[str | None] = mapped_column(String(1024))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
