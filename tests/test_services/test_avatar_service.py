from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import avatar_service


def test_get_or_create_avatars_dedupes_and_creates(db_session: Session) -> None:
    avatars = avatar_service.get_or_create_avatars(db_session, ["マヌカ", "マヌカ", " ラプター "])

    names = sorted(a.name for a in avatars)
    assert names == ["マヌカ", "ラプター"]


def test_get_or_create_avatars_reuses_existing(db_session: Session) -> None:
    first = avatar_service.get_or_create_avatars(db_session, ["Manuka"])
    db_session.commit()

    second = avatar_service.get_or_create_avatars(db_session, ["Manuka"])

    assert first[0].id == second[0].id
