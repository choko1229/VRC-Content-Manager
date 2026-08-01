from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/shops")
def shops_page() -> RedirectResponse:
    # Shop management moved into the 設定 page's "ショップ管理" section --
    # this stays as a redirect for old bookmarks/links.
    return RedirectResponse(url="/settings")
