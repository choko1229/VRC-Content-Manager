from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import tag_service


def test_get_or_create_tags_dedupes_and_creates(db_session: Session) -> None:
    tags = tag_service.get_or_create_tags(db_session, ["衣装", "衣装", " ヘアー ", ""])

    names = sorted(t.name for t in tags)
    assert names == ["ヘアー", "衣装"]


def test_get_or_create_tags_reuses_existing(db_session: Session) -> None:
    first = tag_service.get_or_create_tags(db_session, ["shader"])
    db_session.commit()

    second = tag_service.get_or_create_tags(db_session, ["shader"])

    assert first[0].id == second[0].id


def test_get_or_create_tags_empty_input_returns_empty(db_session: Session) -> None:
    assert tag_service.get_or_create_tags(db_session, []) == []


def test_list_tag_names_sorted(db_session: Session) -> None:
    tag_service.get_or_create_tags(db_session, ["zeta", "alpha"])
    db_session.commit()

    assert tag_service.list_tag_names(db_session) == ["alpha", "zeta"]
