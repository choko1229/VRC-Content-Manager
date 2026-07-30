from __future__ import annotations

from datetime import datetime, timezone

from google.oauth2.credentials import Credentials
from sqlalchemy.orm import Session

from app.core import security
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
    security._fernet.cache_clear()
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
    security._fernet.cache_clear()
    oauth_service.save_credentials(app_db_session, _fake_credentials())

    assert oauth_service.is_connected(app_db_session) is True


def test_save_credentials_keeps_prior_refresh_token_when_omitted(app_db_session: Session) -> None:
    security._fernet.cache_clear()
    oauth_service.save_credentials(app_db_session, _fake_credentials(refresh_token="refresh-abc"))

    refreshed = _fake_credentials(refresh_token=None)
    refreshed.token = "access-new"
    oauth_service.save_credentials(app_db_session, refreshed)

    loaded = oauth_service.load_credentials(app_db_session)
    assert loaded is not None
    assert loaded.token == "access-new"
    assert loaded.refresh_token == "refresh-abc"
