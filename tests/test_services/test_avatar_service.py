from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.avatar import Avatar
from app.models.item import Item
from app.services import avatar_service


def _make_item(db: Session, name: str = "Base Model") -> Item:
    item = Item(name=name)
    db.add(item)
    db.flush()
    return item


def test_set_item_as_avatar_registers_and_can_update(db_session: Session) -> None:
    item = _make_item(db_session, "Manuka.vrm")

    avatar = avatar_service.set_item_as_avatar(db_session, item.id, name="マヌカ", memo="改変可")
    assert avatar.item_id == item.id
    assert avatar.name == "マヌカ"
    assert avatar.memo == "改変可"

    updated = avatar_service.set_item_as_avatar(db_session, item.id, name="マヌカ2", memo=None)
    assert updated.id == avatar.id
    assert updated.name == "マヌカ2"
    assert updated.memo is None


def test_set_item_as_avatar_rejects_duplicate_name(db_session: Session) -> None:
    item_a = _make_item(db_session, "A.vrm")
    item_b = _make_item(db_session, "B.vrm")
    avatar_service.set_item_as_avatar(db_session, item_a.id, name="重複名", memo=None)

    with pytest.raises(IntegrityError):
        avatar_service.set_item_as_avatar(db_session, item_b.id, name="重複名", memo=None)


def test_unset_item_as_avatar_removes_registration(db_session: Session) -> None:
    item = _make_item(db_session)
    avatar_service.set_item_as_avatar(db_session, item.id, name="マヌカ", memo=None)

    avatar_service.unset_item_as_avatar(db_session, item.id)

    assert avatar_service.get_avatar_for_item(db_session, item.id) is None


def test_unset_item_as_avatar_is_a_noop_when_not_registered(db_session: Session) -> None:
    item = _make_item(db_session)
    avatar_service.unset_item_as_avatar(db_session, item.id)  # must not raise


def test_list_avatar_options_excludes_legacy_unlinked_rows(db_session: Session) -> None:
    db_session.add(Avatar(name="レガシータグ"))  # pre-redesign row, no item_id
    item = _make_item(db_session, "Rusk.vrm")
    avatar_service.set_item_as_avatar(db_session, item.id, name="Rusk", memo=None)
    db_session.commit()

    options = avatar_service.list_avatar_options(db_session)

    assert [o.name for o in options] == ["Rusk"]
    assert options[0].item_id == item.id
    assert options[0].compatible_item_count == 0


def test_list_avatar_names_excludes_legacy_unlinked_rows(db_session: Session) -> None:
    db_session.add(Avatar(name="レガシータグ"))
    item = _make_item(db_session, "Rusk.vrm")
    avatar_service.set_item_as_avatar(db_session, item.id, name="Rusk", memo=None)
    db_session.commit()

    assert avatar_service.list_avatar_names(db_session) == ["Rusk"]


def test_resolve_existing_avatars_finds_legacy_and_linked_by_name(db_session: Session) -> None:
    db_session.add(Avatar(name="レガシータグ"))
    item = _make_item(db_session, "Rusk.vrm")
    avatar_service.set_item_as_avatar(db_session, item.id, name="Rusk", memo=None)
    db_session.commit()

    resolved = avatar_service.resolve_existing_avatars(db_session, ["レガシータグ", "Rusk", "Unknown"])

    assert sorted(a.name for a in resolved) == ["Rusk", "レガシータグ"]


def test_resolve_existing_avatars_ignores_unknown_names_without_creating(db_session: Session) -> None:
    resolved = avatar_service.resolve_existing_avatars(db_session, ["NoSuchAvatar"])

    assert resolved == []
    assert db_session.query(Avatar).count() == 0


def test_resolve_avatars_by_ids(db_session: Session) -> None:
    item = _make_item(db_session, "Rusk.vrm")
    avatar = avatar_service.set_item_as_avatar(db_session, item.id, name="Rusk", memo=None)

    resolved = avatar_service.resolve_avatars_by_ids(db_session, [avatar.id, 9999])

    assert [a.id for a in resolved] == [avatar.id]
