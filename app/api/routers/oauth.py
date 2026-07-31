"""Google OAuth start/callback routes.

Browser-redirect based (not JSON), but kept under api/routers per the
project's routing convention -- this is backend integration plumbing, not a
page the rest of the UI links into directly (aside from the setup/settings
pages' "connect" links).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.exceptions import AppError
from app.db.session import get_db
from app.services import drive_sync_service, oauth_service

router = APIRouter(prefix="/oauth/google")
logger = logging.getLogger(__name__)


@router.get("/start")
def start(request: Request):
    try:
        auth_url, state, code_verifier = oauth_service.build_authorization_url()
    except oauth_service.OAuthNotConfiguredError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    request.session["oauth_state"] = state
    request.session["oauth_code_verifier"] = code_verifier
    return RedirectResponse(url=auth_url)


@router.get("/callback")
async def callback(request: Request, code: str, state: str, db: Session = Depends(get_db)):
    expected_state = request.session.pop("oauth_state", None)
    code_verifier = request.session.pop("oauth_code_verifier", None)
    if not expected_state or expected_state != state:
        raise HTTPException(status_code=400, detail="invalid OAuth state (possible CSRF or expired session)")
    if not code_verifier:
        raise HTTPException(status_code=400, detail="OAuthセッションの有効期限が切れました。/setup からやり直してください。")

    try:
        credentials = await run_in_threadpool(
            oauth_service.exchange_code_for_credentials, code=code, state=state, code_verifier=code_verifier
        )
    except AppError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # Anything below our own AppError hierarchy here is almost always the
        # token exchange itself failing (redirect URI mismatch, network
        # issue, an oauthlib scope-mismatch quirk, etc.) -- log the full
        # traceback for diagnosis and give the user an actionable message
        # instead of the generic catch-all 500.
        logger.error("Google token exchange failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Googleとのトークン交換に失敗しました。サーバーログを確認してください。",
        ) from exc

    if drive_sync_service.needs_setup():
        drive_db_file_id = request.session.pop("setup_drive_db_file_id", None)
        await run_in_threadpool(
            drive_sync_service.complete_first_run_setup, credentials, drive_db_file_id=drive_db_file_id
        )
        logger.info("first-run Drive setup completed via OAuth callback")
        return RedirectResponse(url="/")

    oauth_service.save_credentials(db, credentials)
    logger.info("Drive OAuth re-authorized")
    return RedirectResponse(url="/settings")
