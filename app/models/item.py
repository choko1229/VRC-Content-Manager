from __future__ import annotations

import enum
from datetime import date

from sqlalchemy import Boolean, Date, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.avatar import Avatar, item_avatars
from app.models.tag import Tag, item_tags


class ItemCategory(str, enum.Enum):
    """What kind of asset this is -- independent of whether it's registered
    as a selectable base avatar (see Item.as_avatar/avatar_service): most
    items are avatar clothing/accessories (the historical default), but
    some are tools or extensions used to modify an avatar rather than worn
    by one."""

    CLOTHING = "clothing"
    AVATAR = "avatar"
    TOOL = "tool"
    MA_EXTENSION = "ma_extension"
    SHADER_EXTENSION = "shader_extension"
    OTHER = "other"


class Item(TimestampMixin, Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[ItemCategory] = mapped_column(
        Enum(ItemCategory, native_enum=False, length=32), default=ItemCategory.CLOTHING, nullable=False
    )
    product_url: Mapped[str | None] = mapped_column(String(1024))
    download_source_url: Mapped[str | None] = mapped_column(String(1024))
    purchase_date: Mapped[date | None] = mapped_column(Date)
    download_date: Mapped[date | None] = mapped_column(Date)
    price: Mapped[int | None] = mapped_column(Integer)
    file_format: Mapped[str | None] = mapped_column(String(64))
    status_id: Mapped[int | None] = mapped_column(ForeignKey("statuses.id", ondelete="SET NULL"))
    description: Mapped[str | None] = mapped_column(Text)
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
    as_avatar: Mapped["Avatar | None"] = relationship(
        back_populates="base_item", uselist=False, foreign_keys="Avatar.item_id"
    )

    @property
    def primary_file(self) -> "ItemFile | None":
        from app.models.item_file import FileRole

        return next((f for f in self.files if f.file_role == FileRole.PRIMARY), None)

    @property
    def thumbnail_file(self) -> "ItemFile | None":
        from app.models.item_file import FileRole

        return next((f for f in self.files if f.file_role == FileRole.THUMBNAIL), None)

    @property
    def attachment_files(self) -> list["ItemFile"]:
        """Extra downloadable files grouped onto this item -- e.g. a
        different upload that turned out to link to the same BOOTH product
        as this one already does (see item_service.merge_item_into)."""
        from app.models.item_file import FileRole

        return [f for f in self.files if f.file_role == FileRole.ATTACHMENT]
