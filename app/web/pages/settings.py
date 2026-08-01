from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_instance_config, get_settings
from app.core.csrf import verify_csrf
from app.core.instance_config import save as save_instance_config
from app.db.session import get_db
from app.drive import folder_layout
from app.services import app_config_service, app_settings_service, drive_sync_service, oauth_service, shop_service
from app.web.templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)


def _default_redirect_uri(request: Request) -> str:
    return str(request.url.replace(path="/oauth/google/callback", query=""))


@router.get("/setup")
def setup_page(request: Request):
    if not drive_sync_service.needs_setup():
        return RedirectResponse(url="/settings")

    config = get_instance_config()
    edit_requested = request.query_params.get("edit") == "1"
    if config.oauth_configured and not edit_requested:
        return templates.TemplateResponse(request, "setup.html", {"credentials_configured": True})

    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "credentials_configured": False,
            "error": None,
            "form_values": {
                "google_oauth_client_id": config.google_oauth_client_id,
                "google_oauth_redirect_uri": config.google_oauth_redirect_uri or _default_redirect_uri(request),
                "app_login_password": "",
                "drive_db_file_id": "",
            },
        },
    )


@router.post("/setup", dependencies=[Depends(verify_csrf)])
def setup_submit(
    request: Request,
    google_oauth_client_id: str = Form(...),
    google_oauth_client_secret: str = Form(...),
    google_oauth_redirect_uri: str = Form(...),
    app_login_password: str = Form(""),
    drive_db_file_id: str = Form(""),
):
    form_values = {
        "google_oauth_client_id": google_oauth_client_id,
        "google_oauth_redirect_uri": google_oauth_redirect_uri,
        "app_login_password": app_login_password,
        "drive_db_file_id": drive_db_file_id,
    }

    if not google_oauth_client_id.strip() or not google_oauth_client_secret.strip() or not google_oauth_redirect_uri.strip():
        return templates.TemplateResponse(
            request,
            "setup.html",
            {
                "credentials_configured": False,
                "error": "Client ID・Client Secret・リダイレクトURIは必須です。",
                "form_values": form_values,
            },
            status_code=422,
        )

    settings = get_settings()
    config = get_instance_config()
    config.google_oauth_client_id = google_oauth_client_id.strip()
    config.google_oauth_client_secret = google_oauth_client_secret.strip()
    config.google_oauth_redirect_uri = google_oauth_redirect_uri.strip()
    config.app_login_password = app_login_password.strip()
    save_instance_config(settings.data_dir, config)
    logger.info("Google OAuth credentials saved via /setup")

    if drive_db_file_id.strip():
        request.session["setup_drive_db_file_id"] = drive_db_file_id.strip()

    return RedirectResponse(url="/oauth/google/start", status_code=303)


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    if drive_sync_service.needs_setup():
        return RedirectResponse(url="/setup")

    config = get_instance_config()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "connected": oauth_service.is_connected(db),
            "last_pushed_at": app_settings_service.get_setting(db, "drive_db_last_pushed_at"),
            "drive_db_file_id": app_settings_service.get_setting(db, "drive_db_file_id"),
            "sync_interval_seconds": app_config_service.get_sync_interval_seconds(db),
            "max_upload_size_mb": app_config_service.get_max_upload_size_mb(db),
            "is_dirty": drive_sync_service.is_dirty(),
            "google_oauth_client_id": config.google_oauth_client_id,
            "google_oauth_redirect_uri": config.google_oauth_redirect_uri,
            "app_login_password_set": bool(config.app_login_password),
            "shops": shop_service.list_shops(db),
            "root_folder_name": folder_layout.ROOT_FOLDER_NAME,
            "oauth_error": None,
            "password_error": None,
            "operational_error": None,
        },
    )


@router.post("/settings/oauth-credentials", dependencies=[Depends(verify_csrf)])
def update_oauth_credentials(
    request: Request,
    google_oauth_client_id: str = Form(...),
    google_oauth_client_secret: str = Form(""),
    google_oauth_redirect_uri: str = Form(...),
    db: Session = Depends(get_db),
):
    if not google_oauth_client_id.strip() or not google_oauth_redirect_uri.strip():
        return _settings_response(request, db, oauth_error="Client IDとリダイレクトURIは必須です。")

    settings = get_settings()
    config = get_instance_config()
    config.google_oauth_client_id = google_oauth_client_id.strip()
    config.google_oauth_redirect_uri = google_oauth_redirect_uri.strip()
    if google_oauth_client_secret.strip():
        config.google_oauth_client_secret = google_oauth_client_secret.strip()
    save_instance_config(settings.data_dir, config)
    logger.info("Google OAuth credentials updated via /settings")
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/login-password", dependencies=[Depends(verify_csrf)])
def update_login_password(request: Request, app_login_password: str = Form(""), db: Session = Depends(get_db)):
    settings = get_settings()
    config = get_instance_config()
    config.app_login_password = app_login_password.strip()
    save_instance_config(settings.data_dir, config)
    logger.info("login password %s via /settings", "set" if config.app_login_password else "cleared")
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/operational", dependencies=[Depends(verify_csrf)])
def update_operational_settings(
    request: Request,
    max_upload_size_mb: str = Form(...),
    sync_interval_seconds: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        max_mb = int(max_upload_size_mb)
        interval_s = int(sync_interval_seconds)
        if max_mb <= 0 or interval_s <= 0:
            raise ValueError("must be positive")
    except ValueError:
        return _settings_response(request, db, operational_error="数値を正しく入力してください。")

    app_config_service.set_max_upload_size_mb(db, max_mb)
    app_config_service.set_sync_interval_seconds(db, interval_s)
    logger.info("operational settings updated: max_upload_size_mb=%s sync_interval_seconds=%s", max_mb, interval_s)
    return RedirectResponse(url="/settings", status_code=303)


def _settings_response(
    request: Request,
    db: Session,
    *,
    oauth_error: str | None = None,
    password_error: str | None = None,
    operational_error: str | None = None,
):
    config = get_instance_config()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "connected": oauth_service.is_connected(db),
            "last_pushed_at": app_settings_service.get_setting(db, "drive_db_last_pushed_at"),
            "drive_db_file_id": app_settings_service.get_setting(db, "drive_db_file_id"),
            "sync_interval_seconds": app_config_service.get_sync_interval_seconds(db),
            "max_upload_size_mb": app_config_service.get_max_upload_size_mb(db),
            "is_dirty": drive_sync_service.is_dirty(),
            "google_oauth_client_id": config.google_oauth_client_id,
            "google_oauth_redirect_uri": config.google_oauth_redirect_uri,
            "app_login_password_set": bool(config.app_login_password),
            "shops": shop_service.list_shops(db),
            "root_folder_name": folder_layout.ROOT_FOLDER_NAME,
            "oauth_error": oauth_error,
            "password_error": password_error,
            "operational_error": operational_error,
        },
        status_code=422,
    )
