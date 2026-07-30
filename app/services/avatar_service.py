from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.avatar import Avatar


def list_avatar_names(db: Session) -> list[str]:
    return list(db.execute(select(Avatar.name).order_by(Avatar.name)).scalars().all())


def get_or_create_avatars(db: Session, names: list[str]) -> list[Avatar]:
    """Free-text avatar entry, auto-registered into the avatars master on first use."""
    cleaned = sorted({n.strip() for n in names if n.strip()})
    if not cleaned:
        return []

    existing = {a.name: a for a in db.execute(select(Avatar).where(Avatar.name.in_(cleaned))).scalars().all()}
    avatars: list[Avatar] = []
    for name in cleaned:
        avatar = existing.get(name)
        if avatar is None:
            avatar = Avatar(name=name)
            db.add(avatar)
            existing[name] = avatar
        avatars.append(avatar)
    db.flush()
    return avatars
