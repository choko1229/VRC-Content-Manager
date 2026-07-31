from __future__ import annotations

from datetime import datetime, timezone

from google.oauth2.credentials import Credentials
from sqlalchemy.orm import Session

from app.services import oauth_service


def _fake_credentials(*, refresh_token: str | None = "refresh-abc") -> Credentials:
    return Credentials(
        token="access-xyz",
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client-id",
        client_secret="client-secret",
        scopes=oauth_service.SCOPES,
        expiry=datetime(2030, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None),
    )


def test_save_and_load_credentials_round_trip(app_db_session: Session) -> None:
    credentials = _fake_credentials()

    oauth_service.save_credentials(app_db_session, credentials)
    loaded = oauth_service.load_credentials(app_db_session)

    assert loaded is not None
    assert loaded.token == "access-xyz"
    assert loaded.refresh_token == "refresh-abc"
    assert loaded.scopes == oauth_service.SCOPES


def test_is_connected_false_before_any_save(app_db_session: Session) -> None:
    assert oauth_service.is_connected(app_db_session) is False


def test_is_connected_true_after_save(app_db_session: Session) -> None:
    oauth_service.save_credentials(app_db_session, _fake_credentials())

    assert oauth_service.is_connected(app_db_session) is True


def test_save_credentials_keeps_prior_refresh_token_when_omitted(app_db_session: Session) -> None:
    oauth_service.save_credentials(app_db_session, _fake_credentials(refresh_token="refresh-abc"))

    refreshed = _fake_credentials(refresh_token=None)
    refreshed.token = "access-new"
    oauth_service.save_credentials(app_db_session, refreshed)

    loaded = oauth_service.load_credentials(app_db_session)
    assert loaded is not None
    assert loaded.token == "access-new"
    assert loaded.refresh_token == "refresh-abc"


def _configure_oauth_client(configured_settings) -> None:
    from app.config import get_instance_config
    from app.core.instance_config import save as save_instance_config

    config = get_instance_config()
    config.google_oauth_client_id = "test-client-id"
    config.google_oauth_client_secret = "test-client-secret"
    config.google_oauth_redirect_uri = "http://localhost:8000/oauth/google/callback"
    save_instance_config(configured_settings.data_dir, config)


def test_build_authorization_url_returns_a_code_verifier(configured_settings) -> None:
    _configure_oauth_client(configured_settings)

    auth_url, state, code_verifier = oauth_service.build_authorization_url()

    assert auth_url.startswith("https://accounts.google.com/")
    assert state
    assert code_verifier
    assert "code_challenge=" in auth_url  # PKCE is on; the verifier must round-trip to exchange


def test_exchange_uses_the_code_verifier_it_was_given(configured_settings) -> None:
    # Regression test: /start and /callback build separate Flow instances
    # (two different HTTP requests), so the code_verifier generated during
    # authorization_url() must be threaded through explicitly -- otherwise
    # Google's token endpoint rejects the exchange with
    # "invalid_grant: Missing code verifier". This checks the Flow object
    # built for exchange actually carries the verifier, without hitting
    # Google's real token endpoint.
    _configure_oauth_client(configured_settings)
    _, _, code_verifier = oauth_service.build_authorization_url()

    flow = oauth_service._build_flow(state="some-state", code_verifier=code_verifier)

    assert flow.code_verifier == code_verifier
