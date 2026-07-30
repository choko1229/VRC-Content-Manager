from __future__ import annotations

from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.avatar import Avatar, item_avatars
from app.models.tag import Tag, item_tags


class Item(TimestampMixin, Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    product_url: Mapped[str | None] = mapped_column(String(1024))
    download_source_url: Mapped[str | None] = mapped_column(String(1024))
    purchase_date: Mapped[date | None] = mapped_column(Date)
    download_date: Mapped[date | None] = mapped_column(Date)
    price: Mapped[int | None] = mapped_column(Integer)
    file_format: Mapped[str | None] = mapped_column(String(64))
    status_id: Mapped[int | None] = mapped_column(ForeignKey("statuses.id", ondelete="SET NULL"))
    memo: Mapped[str | None] = mapped_column(Text)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    shop: Mapped["Shop"] = relationship(back_populates="items")
    status: Mapped["Status"] = relationship(back_populates="items")
    files: Mapped[list["ItemFile"]] = relationship(back_populates="item", cascade="all, delete-orphan")
    license: Mapped["License | None"] = relationship(
        back_populates="item", cascade="all, delete-orphan", uselist=False
    )
    update_history: Mapped[list["UpdateHistory"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )
    dependencies: Mapped[list["ItemDependency"]] = relationship(
        back_populates="item",
        foreign_keys="ItemDependency.item_id",
        cascade="all, delete-orphan",
    )
    tags: Mapped[list[Tag]] = relationship(secondary=item_tags, back_populates="items")
    avatars: Mapped[list[Avatar]] = relationship(secondary=item_avatars, back_populates="items")

    @property
    def primary_file(self) -> "ItemFile | None":
        from app.models.item_file import FileRole

        return next((f for f in self.files if f.file_role == FileRole.PRIMARY), None)

    @property
    def thumbnail_file(self) -> "ItemFile | None":
        from app.models.item_file import FileRole

        return next((f for f in self.files if f.file_role == FileRole.THUMBNAIL), None)
