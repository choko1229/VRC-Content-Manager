from __future__ import annotations

import time
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.core.csrf import get_csrf_token

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["csrf_token"] = get_csrf_token
# Cache-busting for /static assets: changes on every process start, so a CDN or
# browser cache holding an old style.css/js file gets bypassed after a redeploy.
templates.env.globals["asset_version"] = str(int(time.time()))
