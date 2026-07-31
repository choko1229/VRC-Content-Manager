"""Session-bound CSRF protection for HTMX/form mutations.

A token is minted once per session and exposed to templates via
`csrf_token(request)` (registered as a Jinja global). HTMX requests send it
back as the `X-CSRF-Token` header (wired up once in base.html's
htmx:configRequest listener); plain `<form>` posts (the file-upload ingest
and edit forms) carry it as a hidden `csrf_token` field instead, since HTMX
headers don't apply to a normal browser form submission.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request

_SESSION_KEY = "csrf_token"


def get_csrf_token(request: Request) -> str:
    token = request.session.get(_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[_SESSION_KEY] = token
    return token


async def verify_csrf(request: Request) -> None:
    expected = request.session.get(_SESSION_KEY)
    submitted = request.headers.get("x-csrf-token")

    if not submitted:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            value = form.get("csrf_token")
            submitted = value if isinstance(value, str) else None

    if not expected or not submitted or not secrets.compare_digest(expected, submitted):
        raise HTTPException(status_code=403, detail="CSRFトークンが無効です。ページを再読み込みしてください。")
