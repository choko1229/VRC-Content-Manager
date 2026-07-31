from __future__ import annotations

import pytest

from app.core import security
from app.core.security import TokenEncryptionError, sanitize_filename


def test_encrypt_decrypt_round_trip(configured_settings) -> None:
    ciphertext = security.encrypt_token("super-secret-refresh-token")

    assert ciphertext != "super-secret-refresh-token"
    assert security.decrypt_token(ciphertext) == "super-secret-refresh-token"


def test_decrypt_with_wrong_key_raises(configured_settings) -> None:
    from cryptography.fernet import Fernet

    from app.config import get_instance_config
    from app.core.instance_config import save as save_instance_config

    ciphertext = security.encrypt_token("secret")

    config = get_instance_config()
    config.token_encryption_key = Fernet.generate_key().decode()
    save_instance_config(configured_settings.data_dir, config)

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
