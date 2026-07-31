from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import get_instance_config
from app.core.instance_config import save as save_instance_config
from app.db.session import get_db
from app.main import app
from app.services import oauth_service


@pytest.fixture()
def oauth_client(app_db_session: Session, migrated_settings):
    config = get_instance_config()
    config.google_oauth_client_id = "test-client-id"
    config.google_oauth_client_secret = "test-client-secret"
    config.google_oauth_redirect_uri = "http://testserver/oauth/google/callback"
    save_instance_config(migrated_settings.data_dir, config)

    def override_get_db():
        yield app_db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _obtain_state(client: TestClient) -> str:
    response = client.get("/oauth/google/start", follow_redirects=False)
    assert response.status_code in (302, 307)
    match = re.search(r"[?&]state=([^&]+)", response.headers["location"])
    assert match is not None
    return match.group(1)


def test_callback_rejects_mismatched_state(oauth_client: TestClient) -> None:
    _obtain_state(oauth_client)

    response = oauth_client.get("/oauth/google/callback", params={"code": "abc", "state": "wrong-state"})

    assert response.status_code == 400


def test_callback_returns_502_when_token_exchange_raises_unexpected_error(
    oauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _obtain_state(oauth_client)

    def _boom(*, code: str, state: str, code_verifier: str):
        raise RuntimeError("scope has changed")  # simulates an uncaught oauthlib error

    monkeypatch.setattr(oauth_service, "exchange_code_for_credentials", _boom)

    response = oauth_client.get("/oauth/google/callback", params={"code": "abc", "state": state})

    assert response.status_code == 502
    assert "Google" in response.json()["detail"]


def test_callback_returns_400_when_token_exchange_raises_app_error(
    oauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.exceptions import AppError

    state = _obtain_state(oauth_client)

    def _boom(*, code: str, state: str, code_verifier: str):
        raise AppError("no refresh token")

    monkeypatch.setattr(oauth_service, "exchange_code_for_credentials", _boom)

    response = oauth_client.get("/oauth/google/callback", params={"code": "abc", "state": state})

    assert response.status_code == 400
