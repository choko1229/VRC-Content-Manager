from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.avatar import Avatar, item_avatars
from app.schemas.avatar import AvatarRead
from app.services import drive_sync_service


def _to_read(avatar: Avatar, compatible_item_count: int) -> AvatarRead:
    return AvatarRead(
        id=avatar.id,
        item_id=avatar.item_id,
        name=avatar.name,
        memo=avatar.memo,
        has_thumbnail=avatar.base_item is not None and avatar.base_item.thumbnail_file is not None,
        compatible_item_count=compatible_item_count,
    )


def list_avatar_names(db: Session) -> list[str]:
    """Names of registered (item-linked) avatars, for search/filter autocomplete.

    Legacy free-text rows with no base item (from before avatars were
    unified onto uploaded items) are excluded here -- they're no longer
    manageable through the UI, but resolve_existing_avatars still honors
    them so old items don't lose their existing 対応アバター tags.
    """
    return list(
        db.execute(select(Avatar.name).where(Avatar.item_id.is_not(None)).order_by(Avatar.name)).scalars().all()
    )


def list_avatar_options(db: Session) -> list[AvatarRead]:
    """Registered avatars, for the item-edit checkbox list and the avatar list page."""
    rows = db.execute(
        select(Avatar, func.count(item_avatars.c.item_id))
        .select_from(Avatar)
        .outerjoin(item_avatars, item_avatars.c.avatar_id == Avatar.id)
        .where(Avatar.item_id.is_not(None))
        .options(selectinload(Avatar.base_item))
        .group_by(Avatar.id)
        .order_by(Avatar.name)
    ).all()
    return [_to_read(avatar, count) for avatar, count in rows]


def get_avatar_for_item(db: Session, item_id: int) -> Avatar | None:
    return db.execute(select(Avatar).where(Avatar.item_id == item_id)).scalar_one_or_none()


def set_item_as_avatar(db: Session, item_id: int, *, name: str, memo: str | None) -> Avatar:
    """Register `item_id` as an avatar base model, or update its name/memo if already registered."""
    name = name.strip()
    if not name:
        raise ValueError("avatar name must not be empty")

    avatar = get_avatar_for_item(db, item_id)
    if avatar is None:
        avatar = Avatar(item_id=item_id, name=name, memo=memo or None)
        db.add(avatar)
    else:
        avatar.name = name
        avatar.memo = memo or None
    db.commit()
    db.refresh(avatar)
    drive_sync_service.mark_dirty()
    return avatar


def unset_item_as_avatar(db: Session, item_id: int) -> None:
    avatar = get_avatar_for_item(db, item_id)
    if avatar is not None:
        db.delete(avatar)
        db.commit()
        drive_sync_service.mark_dirty()


def resolve_existing_avatars(db: Session, names: list[str]) -> list[Avatar]:
    """Look up avatars by name for attaching 対応アバター compatibility tags.

    Avatars are no longer freely created from a name list -- the edit UI only
    offers checkboxes for avatars that already exist (list_avatar_options),
    so an unrecognized name here is silently ignored rather than creating a
    new row.
    """
    cleaned = {n.strip() for n in names if n.strip()}
    if not cleaned:
        return []
    return list(db.execute(select(Avatar).where(Avatar.name.in_(cleaned))).scalars().all())


def resolve_avatars_by_ids(db: Session, avatar_ids: list[int]) -> list[Avatar]:
    if not avatar_ids:
        return []
    return list(db.execute(select(Avatar).where(Avatar.id.in_(avatar_ids))).scalars().all())
