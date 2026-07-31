from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import get_instance_config
from app.core.instance_config import save as save_instance_config
from app.db.session import get_db
from app.main import app


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None, "csrf_token hidden field not found in page"
    return match.group(1)


@pytest.fixture()
def setup_client(configured_settings):
    with TestClient(app) as test_client:
        yield test_client


def test_setup_page_shows_credentials_form_when_not_configured(setup_client: TestClient) -> None:
    response = setup_client.get("/setup")

    assert response.status_code == 200
    assert "google_oauth_client_id" in response.text
    assert "Googleでログインして開始" not in response.text


def test_setup_page_shows_connect_button_when_already_configured(setup_client: TestClient, configured_settings) -> None:
    config = get_instance_config()
    config.google_oauth_client_id = "abc.apps.googleusercontent.com"
    config.google_oauth_client_secret = "shh"
    save_instance_config(configured_settings.data_dir, config)

    response = setup_client.get("/setup")

    assert response.status_code == 200
    assert "Googleでログインして開始" in response.text


def test_setup_post_saves_credentials_and_redirects_to_oauth_start(
    setup_client: TestClient, configured_settings
) -> None:
    page = setup_client.get("/setup")
    csrf_token = _extract_csrf(page.text)

    response = setup_client.post(
        "/setup",
        data={
            "google_oauth_client_id": "abc.apps.googleusercontent.com",
            "google_oauth_client_secret": "shh",
            "google_oauth_redirect_uri": "http://localhost:8000/oauth/google/callback",
            "app_login_password": "",
            "drive_db_file_id": "",
        },
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/oauth/google/start"

    config = get_instance_config()
    assert config.google_oauth_client_id == "abc.apps.googleusercontent.com"
    assert config.google_oauth_client_secret == "shh"


def test_setup_post_missing_required_field_returns_422(setup_client: TestClient) -> None:
    page = setup_client.get("/setup")
    csrf_token = _extract_csrf(page.text)

    response = setup_client.post(
        "/setup",
        data={
            "google_oauth_client_id": "",
            "google_oauth_client_secret": "shh",
            "google_oauth_redirect_uri": "http://localhost:8000/oauth/google/callback",
            "app_login_password": "",
            "drive_db_file_id": "",
        },
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 422
    assert not get_instance_config().oauth_configured


@pytest.fixture()
def settings_client(app_db_session: Session):
    def override_get_db():
        yield app_db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _setup_oauth(migrated_settings) -> None:
    config = get_instance_config()
    config.google_oauth_client_id = "initial-id"
    config.google_oauth_client_secret = "initial-secret"
    config.google_oauth_redirect_uri = "http://localhost:8000/oauth/google/callback"
    save_instance_config(migrated_settings.data_dir, config)


def test_update_oauth_credentials_keeps_secret_when_blank(settings_client: TestClient, migrated_settings) -> None:
    _setup_oauth(migrated_settings)
    page = settings_client.get("/settings")
    csrf_token = _extract_csrf(page.text)

    response = settings_client.post(
        "/settings/oauth-credentials",
        data={
            "google_oauth_client_id": "updated-id",
            "google_oauth_client_secret": "",
            "google_oauth_redirect_uri": "http://localhost:8000/oauth/google/callback",
        },
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    config = get_instance_config()
    assert config.google_oauth_client_id == "updated-id"
    assert config.google_oauth_client_secret == "initial-secret"  # kept, not blanked out


def test_update_login_password_sets_and_clears(settings_client: TestClient, migrated_settings) -> None:
    _setup_oauth(migrated_settings)
    page = settings_client.get("/settings")
    csrf_token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)

    settings_client.post(
        "/settings/login-password",
        data={"app_login_password": "new-password"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert get_instance_config().app_login_password == "new-password"

    # Setting a password takes effect immediately, including for the current
    # session -- the gate has no way to know this session was "already"
    # trusted, so it must re-authenticate before it can change anything else.
    login_page = settings_client.get("/login")
    login_csrf = _extract_csrf(login_page.text)
    settings_client.post(
        "/login",
        data={"password": "new-password"},
        headers={"X-CSRF-Token": login_csrf},
        follow_redirects=False,
    )

    settings_client.post(
        "/settings/login-password",
        data={"app_login_password": ""},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert get_instance_config().app_login_password == ""


def test_update_operational_settings(settings_client: TestClient, migrated_settings, app_db_session: Session) -> None:
    _setup_oauth(migrated_settings)
    page = settings_client.get("/settings")
    csrf_token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)

    response = settings_client.post(
        "/settings/operational",
        data={"max_upload_size_mb": "250", "sync_interval_seconds": "120"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    from app.services import app_config_service

    assert app_config_service.get_max_upload_size_mb(app_db_session) == 250
    assert app_config_service.get_sync_interval_seconds(app_db_session) == 120


def test_update_operational_settings_rejects_non_positive_values(
    settings_client: TestClient, migrated_settings
) -> None:
    _setup_oauth(migrated_settings)
    page = settings_client.get("/settings")
    csrf_token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)

    response = settings_client.post(
        "/settings/operational",
        data={"max_upload_size_mb": "0", "sync_interval_seconds": "60"},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 422
