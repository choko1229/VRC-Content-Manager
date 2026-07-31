from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app.config import get_instance_config
from app.core.csrf import verify_csrf
from app.web.templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/login")
def login_page(request: Request):
    if not get_instance_config().app_login_password:
        return RedirectResponse(url="/")
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "パスワードが違います。" if request.query_params.get("error") else None},
    )


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login_submit(request: Request, password: str = Form(...)):
    expected = get_instance_config().app_login_password
    next_url = request.query_params.get("next") or "/"

    if expected and secrets.compare_digest(password, expected):
        request.session["authenticated"] = True
        logger.info("login successful")
        return RedirectResponse(url=next_url, status_code=303)

    logger.warning("login failed (incorrect password)")
    return RedirectResponse(url="/login?error=1", status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.pop("authenticated", None)
    return RedirectResponse(url="/login", status_code=303)
