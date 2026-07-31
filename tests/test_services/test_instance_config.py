from __future__ import annotations

from pathlib import Path

from app.core import instance_config


def test_load_auto_generates_config_on_first_use(tmp_path: Path) -> None:
    config = instance_config.load(tmp_path)

    assert config.token_encryption_key
    assert config.session_secret_key
    assert config.google_oauth_client_id == ""
    assert config.oauth_configured is False
    assert (tmp_path / "instance_config.json").exists()


def test_load_returns_same_keys_on_subsequent_calls(tmp_path: Path) -> None:
    first = instance_config.load(tmp_path)
    second = instance_config.load(tmp_path)

    assert first.token_encryption_key == second.token_encryption_key
    assert first.session_secret_key == second.session_secret_key


def test_save_persists_changes(tmp_path: Path) -> None:
    config = instance_config.load(tmp_path)
    config.google_oauth_client_id = "abc.apps.googleusercontent.com"
    config.google_oauth_client_secret = "shh"

    instance_config.save(tmp_path, config)
    reloaded = instance_config.load(tmp_path)

    assert reloaded.google_oauth_client_id == "abc.apps.googleusercontent.com"
    assert reloaded.google_oauth_client_secret == "shh"
    assert reloaded.oauth_configured is True


def test_oauth_configured_requires_both_id_and_secret(tmp_path: Path) -> None:
    config = instance_config.load(tmp_path)
    config.google_oauth_client_id = "only-id"

    assert config.oauth_configured is False

    config.google_oauth_client_secret = "and-a-secret"
    assert config.oauth_configured is True


def test_generated_token_encryption_key_is_a_valid_fernet_key(tmp_path: Path) -> None:
    from cryptography.fernet import Fernet

    config = instance_config.load(tmp_path)

    Fernet(config.token_encryption_key.encode())  # raises if invalid
