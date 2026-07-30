from __future__ import annotations

import enum

from sqlalchemy import Enum, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class TriState(str, enum.Enum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class License(Base):
    __tablename__ = "licenses"

    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    commercial_use: Mapped[TriState] = mapped_column(
        Enum(TriState, native_enum=False, length=16), default=TriState.UNKNOWN, nullable=False
    )
    modification_allowed: Mapped[TriState] = mapped_column(
        Enum(TriState, native_enum=False, length=16), default=TriState.UNKNOWN, nullable=False
    )
    redistribution_allowed: Mapped[TriState] = mapped_column(
        Enum(TriState, native_enum=False, length=16), default=TriState.UNKNOWN, nullable=False
    )
    credit_required: Mapped[TriState] = mapped_column(
        Enum(TriState, native_enum=False, length=16), default=TriState.UNKNOWN, nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)

    item: Mapped["Item"] = relationship(back_populates="license")
