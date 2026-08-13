from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class SavedFilter(Base):
    """A named snapshot of the TOP page's filter form (see items/list.html) --
    the search/絞り込み query string only, never the view/density display
    prefs (those are per-device localStorage, not something worth syncing).
    Lets a frequently-used combination of filters be reapplied with one
    click instead of re-entering it (Eagle's "smart folder" equivalent).
    """

    __tablename__ = "saved_filters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    query_string: Mapped[str] = mapped_column(String(2048), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
