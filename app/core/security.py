"""Token encryption and filename sanitization.

TOKEN_ENCRYPTION_KEY protects the Drive OAuth token at rest in SQLite. This
is defense against exposure of the DB file itself (e.g. a leaked Drive
share); ultimate security still rests on host + Google account security,
which is an accepted trade-off for a single-user personal tool.
"""

from __future__ import annotations

import re
import uuid
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings
from app.core.exceptions import AppError

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9 ._\-()\[\]ぁ-んァ-ヶ一-龠ー]+")


class TokenEncryptionError(AppError):
    pass


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().token_encryption_key
    if not key:
        raise TokenEncryptionError(
            "TOKEN_ENCRYPTION_KEY is not set; cannot encrypt/decrypt the Drive OAuth token. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise TokenEncryptionError("TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc


def encrypt_token(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise TokenEncryptionError("stored token could not be decrypted (wrong key?)") from exc


def sanitize_filename(original_filename: str, *, prefix: str | None = None) -> str:
    """Produce a safe filename for local/Drive storage.

    Never trust the client-supplied filename for storage paths: strip any
    directory components, drop characters outside a conservative allowlist,
    and prefix with a uuid (or caller-supplied prefix) to avoid collisions.
    """
    base = original_filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", base).strip("._ ") or "file"
    stem = prefix or uuid.uuid4().hex
    return f"{stem}_{cleaned}"
