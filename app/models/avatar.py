from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Table, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow

item_avatars = Table(
    "item_avatars",
    Base.metadata,
    Column("item_id", ForeignKey("items.id", ondelete="CASCADE"), primary_key=True),
    Column("avatar_id", ForeignKey("avatars.id", ondelete="CASCADE"), primary_key=True),
)


class Avatar(Base):
    """An avatar base model, always backed by one uploaded Item (`base_item`).

    `item_id` is nullable only to preserve pre-redesign rows that had no
    backing item (free-text tags from before avatars were unified onto
    uploaded items) -- the app no longer creates or surfaces avatars without
    one; see avatar_service.list_avatar_options.
    """

    __tablename__ = "avatars"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), unique=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    memo: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    base_item: Mapped["Item | None"] = relationship(back_populates="as_avatar", foreign_keys=[item_id])
    items: Mapped[list["Item"]] = relationship(secondary=item_avatars, back_populates="avatars")
