from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, ValidationError
from app.services import saved_filter_service


def test_create_and_list_saved_filter(db_session: Session) -> None:
    saved_filter_service.create_saved_filter(db_session, "夏の水着", "tags=水着&favorites_only=true")

    filters = saved_filter_service.list_saved_filters(db_session)

    assert len(filters) == 1
    assert filters[0].name == "夏の水着"
    assert filters[0].query_string == "tags=水着&favorites_only=true"


def test_create_saved_filter_rejects_blank_name(db_session: Session) -> None:
    with pytest.raises(ValidationError):
        saved_filter_service.create_saved_filter(db_session, "   ", "q=test")


def test_create_saved_filter_with_existing_name_overwrites_query(db_session: Session) -> None:
    first = saved_filter_service.create_saved_filter(db_session, "お気に入り", "favorites_only=true")

    updated = saved_filter_service.create_saved_filter(db_session, "お気に入り", "favorites_only=true&category=avatar")

    assert updated.id == first.id  # same row, not a duplicate
    filters = saved_filter_service.list_saved_filters(db_session)
    assert len(filters) == 1
    assert filters[0].query_string == "favorites_only=true&category=avatar"


def test_delete_saved_filter(db_session: Session) -> None:
    created = saved_filter_service.create_saved_filter(db_session, "削除予定", "q=x")

    saved_filter_service.delete_saved_filter(db_session, created.id)

    assert saved_filter_service.list_saved_filters(db_session) == []


def test_delete_saved_filter_raises_not_found(db_session: Session) -> None:
    with pytest.raises(NotFoundError):
        saved_filter_service.delete_saved_filter(db_session, 999)
