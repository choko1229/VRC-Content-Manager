from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, ValidationError
from app.models.tag import Tag
from app.schemas.tag import TagRead
from app.services import drive_sync_service

logger = logging.getLogger(__name__)


def _to_read(tag: Tag) -> TagRead:
    return TagRead(id=tag.id, name=tag.name, item_count=len(tag.items))


def list_tag_names(db: Session) -> list[str]:
    return list(db.execute(select(Tag.name).order_by(Tag.name)).scalars().all())


def list_tags(db: Session) -> list[TagRead]:
    tags = db.execute(select(Tag).order_by(Tag.name)).scalars().all()
    return [_to_read(t) for t in tags]


def get_tag(db: Session, tag_id: int) -> Tag:
    tag = db.get(Tag, tag_id)
    if tag is None:
        raise NotFoundError("Tag", tag_id)
    return tag


def _merge(db: Session, *, source: Tag, target: Tag) -> TagRead:
    for item in list(source.items):
        if target not in item.tags:
            item.tags.append(target)
        item.tags.remove(source)
    db.delete(source)
    db.commit()
    db.refresh(target)
    drive_sync_service.mark_dirty()
    logger.info("tag merged source_id=%s into target_id=%s", source.id, target.id)
    return _to_read(target)


def rename_or_merge_tag(db: Session, tag_id: int, new_name: str) -> TagRead:
    """Renames the tag to new_name. If another tag already has that name
    (Tag.name is unique), merges into it instead -- every item tagged with
    the source picks up the target tag (if it doesn't have it already) and
    loses the source, then the now-empty source tag is deleted. This is the
    tag management screen's only edit control: typing an existing tag's
    name is how you merge a typo/duplicate, no separate merge picker
    needed.
    """
    tag = get_tag(db, tag_id)
    new_name = new_name.strip()
    if not new_name:
        raise ValidationError("タグ名を入力してください。")
    if new_name == tag.name:
        return _to_read(tag)

    existing = db.execute(select(Tag).where(Tag.name == new_name, Tag.id != tag_id)).scalar_one_or_none()
    if existing is None:
        tag.name = new_name
        db.commit()
        db.refresh(tag)
        drive_sync_service.mark_dirty()
        logger.info("tag renamed id=%s new_name=%s", tag_id, new_name)
        return _to_read(tag)

    return _merge(db, source=tag, target=existing)


def delete_tag(db: Session, tag_id: int) -> None:
    tag = get_tag(db, tag_id)
    db.delete(tag)
    db.commit()
    drive_sync_service.mark_dirty()
    logger.info("tag deleted id=%s", tag_id)


def get_or_create_tags(db: Session, names: list[str]) -> list[Tag]:
    """Free-text tags, auto-registered into the tags master on first use."""
    cleaned = sorted({n.strip() for n in names if n.strip()})
    if not cleaned:
        return []

    existing = {t.name: t for t in db.execute(select(Tag).where(Tag.name.in_(cleaned))).scalars().all()}
    tags: list[Tag] = []
    for name in cleaned:
        tag = existing.get(name)
        if tag is None:
            tag = Tag(name=name)
            db.add(tag)
            existing[name] = tag
        tags.append(tag)
    db.flush()
    return tags
