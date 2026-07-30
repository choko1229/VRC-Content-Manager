from __future__ import annotations

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ItemDependency(Base):
    """A dependency of `item_id` on another asset.

    Either `depends_on_item_id` (a tracked item, e.g. a required shader) or
    `free_text_note` (an untracked dependency, e.g. "needs XYZ shader v3
    from a different shop, not yet registered here") should be set.
    """

    __tablename__ = "item_dependencies"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    depends_on_item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"))
    free_text_note: Mapped[str | None] = mapped_column(Text)

    item: Mapped["Item"] = relationship(foreign_keys=[item_id], back_populates="dependencies")
    depends_on_item: Mapped["Item | None"] = relationship(foreign_keys=[depends_on_item_id])
