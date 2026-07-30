"""Application configuration.

Secrets and deployment-environment values come from `.env` / real environment
variables via pydantic-settings. Anything that is more "operational setting"
than "secret" (upload extension allowlist overrides, default status, etc.)
belongs in the DB-backed `app_settings` table instead, per the project's
config policy — see app/services/settings_service.py (added in a later phase).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Google OAuth ---
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = "http://localhost:8000/oauth/google/callback"

    # --- Secrets ---
    token_encryption_key: str = ""
    session_secret_key: str = "dev-insecure-session-secret-change-me"
    app_login_password: str = ""

    # --- Disaster recovery ---
    drive_db_file_id: str = ""

    # --- Runtime ---
    data_dir: Path = Field(default=Path("./data"))
    port: int = 8000
    log_level: str = "INFO"
    max_upload_size_mb: int = 500
    sync_interval_seconds: int = 60

    @property
    def local_db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def upload_tmp_dir(self) -> Path:
        return self.data_dir / "tmp" / "uploads"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.local_db_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
