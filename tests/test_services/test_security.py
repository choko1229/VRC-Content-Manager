from __future__ import annotations

import pytest

from app.core import security
from app.core.security import TokenEncryptionError, sanitize_filename


def test_encrypt_decrypt_round_trip(configured_settings) -> None:
    security._fernet.cache_clear()

    ciphertext = security.encrypt_token("super-secret-refresh-token")

    assert ciphertext != "super-secret-refresh-token"
    assert security.decrypt_token(ciphertext) == "super-secret-refresh-token"


def test_decrypt_with_wrong_key_raises(configured_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    security._fernet.cache_clear()
    ciphertext = security.encrypt_token("secret")

    from cryptography.fernet import Fernet

    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings

    get_settings.cache_clear()
    security._fernet.cache_clear()

    with pytest.raises(TokenEncryptionError):
        security.decrypt_token(ciphertext)


@pytest.mark.parametrize(
    "original,expected_suffix",
    [
        ("my avatar.unitypackage", "my avatar.unitypackage"),
        ("../../etc/passwd", "passwd"),
        ("C:\\weird\\path\\file.vrm", "file.vrm"),
        ("<script>.zip", "script_.zip"),
    ],
)
def test_sanitize_filename_strips_path_and_unsafe_chars(original: str, expected_suffix: str) -> None:
    result = sanitize_filename(original, prefix="fixed")

    assert result == f"fixed_{expected_suffix}"
    assert "/" not in result
    assert "\\" not in result
