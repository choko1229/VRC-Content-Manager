from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, ValidationError
from app.models.saved_filter import SavedFilter
from app.schemas.saved_filter import SavedFilterRead
from app.services import drive_sync_service

logger = logging.getLogger(__name__)


def _to_read(saved: SavedFilter) -> SavedFilterRead:
    return SavedFilterRead(id=saved.id, name=saved.name, query_string=saved.query_string)


def list_saved_filters(db: Session) -> list[SavedFilterRead]:
    rows = db.execute(select(SavedFilter).order_by(SavedFilter.name)).scalars().all()
    return [_to_read(r) for r in rows]


def create_saved_filter(db: Session, name: str, query_string: str) -> SavedFilterRead:
    name = name.strip()
    if not name:
        raise ValidationError("名前を入力してください。")

    existing = db.execute(select(SavedFilter).where(SavedFilter.name == name)).scalar_one_or_none()
    if existing is not None:
        # Overwrite rather than reject -- re-saving under a name you've
        # already used is how you'd expect to update a saved view's
        # criteria, not something that should require deleting it first.
        existing.query_string = query_string
        db.commit()
        db.refresh(existing)
        drive_sync_service.mark_dirty()
        logger.info("saved filter updated id=%s name=%s", existing.id, name)
        return _to_read(existing)

    saved = SavedFilter(name=name, query_string=query_string)
    db.add(saved)
    db.commit()
    db.refresh(saved)
    drive_sync_service.mark_dirty()
    logger.info("saved filter created id=%s name=%s", saved.id, name)
    return _to_read(saved)


def delete_saved_filter(db: Session, saved_filter_id: int) -> None:
    saved = db.get(SavedFilter, saved_filter_id)
    if saved is None:
        raise NotFoundError("SavedFilter", saved_filter_id)
    db.delete(saved)
    db.commit()
    drive_sync_service.mark_dirty()
    logger.info("saved filter deleted id=%s", saved_filter_id)
