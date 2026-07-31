from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.core.csrf import get_csrf_token

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["csrf_token"] = get_csrf_token
