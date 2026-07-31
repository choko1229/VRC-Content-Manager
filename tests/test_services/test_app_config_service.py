from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import app_config_service


def test_max_upload_size_mb_defaults(db_session: Session) -> None:
    assert app_config_service.get_max_upload_size_mb(db_session) == app_config_service.DEFAULT_MAX_UPLOAD_SIZE_MB


def test_max_upload_size_mb_set_and_get(db_session: Session) -> None:
    app_config_service.set_max_upload_size_mb(db_session, 250)

    assert app_config_service.get_max_upload_size_mb(db_session) == 250


def test_sync_interval_seconds_defaults(db_session: Session) -> None:
    assert app_config_service.get_sync_interval_seconds(db_session) == app_config_service.DEFAULT_SYNC_INTERVAL_SECONDS


def test_sync_interval_seconds_set_and_get(db_session: Session) -> None:
    app_config_service.set_sync_interval_seconds(db_session, 120)

    assert app_config_service.get_sync_interval_seconds(db_session) == 120
