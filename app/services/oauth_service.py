"""Google OAuth token exchange and persistence for the single owner account.

Tokens are stored in `oauth_credentials` (encrypted, see app/core/security.py),
not in `.env` -- they're routinely-updated runtime state, not static secret
config. There is exactly one row (provider="google_drive"); this is a
single-user tool, not multi-tenant.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.exceptions import AppError
from app.core.security import decrypt_token, encrypt_token
from app.drive.google_drive_client import GoogleDriveClient
from app.models.oauth_credential import OAuthCredential

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
PROVIDER = "google_drive"


class OAuthNotConfiguredError(AppError):
    pass


class NotConnectedError(AppError):
    pass


def _client_config() -> dict:
    settings = get_settings()
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise OAuthNotConfiguredError(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set in .env"
        )
    return {
        "web": {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "redirect_uris": [settings.google_oauth_redirect_uri],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def _build_flow(state: str | None = None) -> Flow:
    settings = get_settings()
    return Flow.from_client_config(
        _client_config(),
        scopes=SCOPES,
        state=state,
        redirect_uri=settings.google_oauth_redirect_uri,
    )


def build_authorization_url() -> tuple[str, str]:
    """Returns (authorization_url, state). Caller must keep `state` (e.g. in the session)
    and pass it back to exchange_code_for_credentials for CSRF protection."""
    flow = _build_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",  # guarantees a refresh_token even on repeat authorization
        include_granted_scopes="true",
    )
    return auth_url, state


def exchange_code_for_credentials(*, code: str, state: str) -> Credentials:
    flow = _build_flow(state=state)
    flow.fetch_token(code=code)
    credentials = flow.credentials
    if not credentials.refresh_token:
        raise AppError(
            "Google did not return a refresh_token. This can happen if the app was already "
            "authorized without revoking access first; revoke access at "
            "https://myaccount.google.com/permissions and try again."
        )
    return credentials


def _naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def save_credentials(db: Session, credentials: Credentials) -> None:
    row = db.execute(select(OAuthCredential).where(OAuthCredential.provider == PROVIDER)).scalar_one_or_none()
    if row is None:
        row = OAuthCredential(
            provider=PROVIDER,
            access_token_encrypted="",
            refresh_token_encrypted="",
        )
        db.add(row)

    row.access_token_encrypted = encrypt_token(credentials.token)
    if credentials.refresh_token:
        row.refresh_token_encrypted = encrypt_token(credentials.refresh_token)
    elif not row.refresh_token_encrypted:
        raise AppError("no refresh_token available to persist (first-time save must include one)")
    row.token_expiry = _aware_utc(credentials.expiry)
    row.scope = " ".join(credentials.scopes or SCOPES)
    db.commit()
    logger.info("Drive OAuth credentials saved (expiry=%s)", row.token_expiry)


def load_credentials(db: Session) -> Credentials | None:
    row = db.execute(select(OAuthCredential).where(OAuthCredential.provider == PROVIDER)).scalar_one_or_none()
    if row is None:
        return None
    settings = get_settings()
    return Credentials(
        token=decrypt_token(row.access_token_encrypted),
        refresh_token=decrypt_token(row.refresh_token_encrypted),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
        scopes=row.scope.split() if row.scope else SCOPES,
        expiry=_naive_utc(row.token_expiry),
    )


def is_connected(db: Session) -> bool:
    return load_credentials(db) is not None


def make_drive_client(db: Session) -> GoogleDriveClient:
    credentials = load_credentials(db)
    if credentials is None:
        raise NotConnectedError("Google Drive is not connected yet; visit /setup or /settings")

    def _on_refreshed(new_credentials: Credentials) -> None:
        save_credentials(db, new_credentials)

    return GoogleDriveClient(credentials, on_credentials_refreshed=_on_refreshed)
