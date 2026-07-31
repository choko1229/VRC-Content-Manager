"""DB-backed operational settings (as opposed to app/core/instance_config.py's
local-only secrets, or app/config.py's env-only bootstrap values).

These are safe to sync to Drive along with the rest of the database, and are
editable any time after first-run setup via the /settings page.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import app_settings_service

_KEY_MAX_UPLOAD_SIZE_MB = "max_upload_size_mb"
_KEY_SYNC_INTERVAL_SECONDS = "sync_interval_seconds"

DEFAULT_MAX_UPLOAD_SIZE_MB = 500
DEFAULT_SYNC_INTERVAL_SECONDS = 60


def get_max_upload_size_mb(db: Session) -> int:
    raw = app_settings_service.get_setting(db, _KEY_MAX_UPLOAD_SIZE_MB)
    return int(raw) if raw else DEFAULT_MAX_UPLOAD_SIZE_MB


def set_max_upload_size_mb(db: Session, value: int) -> None:
    app_settings_service.set_setting(db, _KEY_MAX_UPLOAD_SIZE_MB, str(value))


def get_sync_interval_seconds(db: Session) -> int:
    raw = app_settings_service.get_setting(db, _KEY_SYNC_INTERVAL_SECONDS)
    return int(raw) if raw else DEFAULT_SYNC_INTERVAL_SECONDS


def set_sync_interval_seconds(db: Session, value: int) -> None:
    app_settings_service.set_setting(db, _KEY_SYNC_INTERVAL_SECONDS, str(value))
