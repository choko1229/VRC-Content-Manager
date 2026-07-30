from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tag import Tag


def list_tag_names(db: Session) -> list[str]:
    return list(db.execute(select(Tag.name).order_by(Tag.name)).scalars().all())


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
