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
        auth_url, state = oauth_service.build_authorization_url()
    except oauth_service.OAuthNotConfiguredError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    request.session["oauth_state"] = state
    return RedirectResponse(url=auth_url)


@router.get("/callback")
async def callback(request: Request, code: str, state: str, db: Session = Depends(get_db)):
    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or expected_state != state:
        raise HTTPException(status_code=400, detail="invalid OAuth state (possible CSRF or expired session)")

    try:
        credentials = await run_in_threadpool(
            oauth_service.exchange_code_for_credentials, code=code, state=state
        )
    except AppError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if drive_sync_service.needs_setup():
        await run_in_threadpool(drive_sync_service.complete_first_run_setup, credentials)
        logger.info("first-run Drive setup completed via OAuth callback")
        return RedirectResponse(url="/")

    oauth_service.save_credentials(db, credentials)
    logger.info("Drive OAuth re-authorized")
    return RedirectResponse(url="/settings")
