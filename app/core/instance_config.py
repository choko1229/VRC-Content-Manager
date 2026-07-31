"""Local, Drive-independent instance configuration.

Per the project's config policy, `.env` holds only what must be known before
any file can even be located (`DATA_DIR`) or before the app can bind a port
(`PORT`), plus `LOG_LEVEL` as a debugging-only exception. Everything else
that used to live in `.env` -- the Google OAuth client credentials, the
token-encryption/session-signing keys, and the optional login password --
lives here instead: a small JSON file at `${DATA_DIR}/instance_config.json`,
populated by the /setup wizard (OAuth credentials, login password) or
auto-generated on first use (the two secret keys).

This file is deliberately kept OUT of the Drive-synced SQLite database and
is never uploaded to Drive: anyone with read access to the Drive-hosted DB
backup gets the (encrypted) Drive token, but not the OAuth client secret or
the app's login password, which only ever exist on the local disk.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_FILENAME = "instance_config.json"


class InstanceConfig(BaseModel):
    token_encryption_key: str
    session_secret_key: str
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = ""
    app_login_password: str = ""

    @property
    def oauth_configured(self) -> bool:
        return bool(self.google_oauth_client_id and self.google_oauth_client_secret)


def _path(data_dir: Path) -> Path:
    return data_dir / _FILENAME


def _write(path: Path, config: InstanceConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)  # best-effort; Windows ACLs don't honor POSIX chmod bits
    except OSError:
        pass


def load(data_dir: Path) -> InstanceConfig:
    """Load the instance config, auto-generating the secret keys on first use.

    Never raises for a missing file -- a fresh instance always gets one
    created here rather than requiring a manual setup step.
    """
    path = _path(data_dir)
    if path.exists():
        return InstanceConfig.model_validate_json(path.read_text(encoding="utf-8"))

    config = InstanceConfig(
        token_encryption_key=Fernet.generate_key().decode(),
        session_secret_key=secrets.token_urlsafe(32),
    )
    _write(path, config)
    logger.info("generated new instance config at %s", path)
    return config


def save(data_dir: Path, config: InstanceConfig) -> None:
    _write(_path(data_dir), config)
