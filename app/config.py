"""Application configuration.

`.env` is intentionally minimal: only values needed before the app can even
locate its own data or bind a port belong here. Everything else that might
look like ".env material" at first glance lives elsewhere on purpose:
  - Google OAuth client credentials, the token-encryption/session-signing
    keys, and the optional login password -> app/core/instance_config.py
    (a local JSON file, populated via /setup, never synced to Drive).
  - Upload size limit, Drive sync interval -> the DB-backed `app_settings`
    table (app/services/app_settings_service.py), editable from /settings
    once the database exists.
`LOG_LEVEL` is the one debugging-only exception kept as an optional env var:
logging must be configured before any DB read is possible.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.instance_config import InstanceConfig
from app.core.instance_config import load as load_instance_config


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default=Path("./data"))
    port: int = 8000
    log_level: str = "INFO"

    @property
    def local_db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def upload_tmp_dir(self) -> Path:
        return self.data_dir / "tmp" / "uploads"

    @property
    def pending_upload_cache_dir(self) -> Path:
        """Holds a file between "accepted the upload" and "confirmed pushed
        to Drive" (see upload_sync_service). Persistent, not time-limited --
        entries are removed the moment their Drive push succeeds."""
        return self.data_dir / "cache" / "uploads"

    @property
    def download_cache_dir(self) -> Path:
        """A time-limited (see local_cache_service) local cache of files
        fetched from Drive, keyed by drive_file_id, so repeat downloads and
        thumbnail views don't re-fetch from Drive every time."""
        return self.data_dir / "cache" / "downloads"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.local_db_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_instance_config() -> InstanceConfig:
    """Not cached: this is a small local JSON file re-read on each call so that
    edits made via /setup or /settings take effect immediately without a restart."""
    return load_instance_config(get_settings().data_dir)
