from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from sqlalchemy.orm import Session

from app.services import booth_library_service


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    # follow_redirects=True to mirror make_client() (see booth_common) --
    # needed so the sign-in-redirect tests below see the same final-URL
    # behavior production code does.
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def _allow_robots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(booth_library_service, "robots_allow", lambda url: True)


def _library_card(product_id: str, name: str, shop_name: str, shop_id: str, filenames: list[str]) -> str:
    # Trimmed-down version of accounts.booth.pm/library's real markup
    # (verified against a real account's page): the utility-class-only divs
    # here are exactly what BeautifulSoup selectors in _parse_page target.
    file_rows = "".join(
        f'<div class="mt-16 desktop:flex desktop:justify-between desktop:items-center">'
        f'<div class="min-w-0 break-words whitespace-pre-line"><div class="text-14">{fn}</div></div>'
        f"</div>"
        for fn in filenames
    )
    return (
        '<div class="bg-white p-16 desktop:rounded-8 desktop:py-24 desktop:px-40">'
        '<div class="flex gap-8 desktop:gap-16 border-b border-border300 pb-16">'
        f'<a href="https://booth.pm/ja/items/{product_id}" rel="noopener" target="_blank">'
        f'<img class="l-library-item-thumbnail" src="https://booth.pximg.net/thumb-{product_id}.jpg"/>'
        "</a>"
        "<div>"
        f'<a class="no-underline" href="https://booth.pm/ja/items/{product_id}" rel="noopener" target="_blank">'
        f'<div class="text-text-default font-bold text-16 mb-8 break-all">{name}</div>'
        "</a>"
        f'<a class="no-underline w-fit flex gap-4 items-center" href="https://{shop_id}.booth.pm/" rel="noopener" target="_blank">'
        f'<div class="text-14 text-text-gray600 break-all">{shop_name}</div>'
        "</a>"
        "</div>"
        "</div>"
        f'<div class="mt-16">{file_rows}</div>'
        "</div>"
    )


def _library_page(cards: list[str]) -> str:
    return f'<html><body><div class="w-full">{"".join(cards)}</div></body></html>'


def _sign_in_redirect_response(request: httpx.Request) -> httpx.Response:
    # Mirrors BOOTH's real behavior for an expired/invalid session: the
    # library request 303s to the login page, which make_client()'s
    # follow_redirects=True then resolves to a 200 whose *final* URL is
    # /users/sign_in -- MockTransport re-invokes this handler once per hop,
    # so branch on the path being requested this time.
    if request.url.path == "/users/sign_in":
        return httpx.Response(200, text="<html><body>sign in</body></html>", headers={"content-type": "text/html"})
    return httpx.Response(303, headers={"location": "https://accounts.booth.pm/users/sign_in"})


def test_parse_page_extracts_product_and_filenames() -> None:
    html = _library_page(
        [_library_card("111", "Cool Avatar", "Cool Shop", "coolshop", ["Cool_Avatar_v1.zip", "Cool_Avatar_v2.zip"])]
    )

    entries = booth_library_service._parse_page(html)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.product_url == "https://booth.pm/ja/items/111"
    assert entry.product_name == "Cool Avatar"
    assert entry.shop_name == "Cool Shop"
    assert entry.shop_url == "https://coolshop.booth.pm/"
    assert entry.thumbnail_url == "https://booth.pximg.net/thumb-111.jpg"
    assert entry.filenames == ["Cool_Avatar_v1.zip", "Cool_Avatar_v2.zip"]


def test_parse_page_handles_multiple_cards() -> None:
    html = _library_page(
        [
            _library_card("111", "First", "Shop A", "shopa", ["a.zip"]),
            _library_card("222", "Second", "Shop B", "shopb", ["b1.zip", "b2.zip"]),
        ]
    )

    entries = booth_library_service._parse_page(html)

    assert [e.product_name for e in entries] == ["First", "Second"]
    assert entries[1].filenames == ["b1.zip", "b2.zip"]


def test_parse_page_returns_empty_list_for_page_with_no_cards() -> None:
    assert booth_library_service._parse_page(_library_page([])) == []


def test_fetch_library_paginates_until_an_empty_page(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)
    pages_served = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        pages_served.append(page)
        if page == "1":
            html = _library_page([_library_card("111", "First", "Shop A", "shopa", ["a.zip"])])
        else:
            html = _library_page([])
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    entries = booth_library_service.fetch_library("session=abc", client=_client(handler))

    assert [e.product_name for e in entries] == ["First"]
    assert pages_served == ["1", "2"]


def test_fetch_library_sends_the_cookie_header(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)
    seen_cookies = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_cookies.append(request.headers.get("cookie"))
        return httpx.Response(200, text=_library_page([]), headers={"content-type": "text/html"})

    booth_library_service.fetch_library("session=abc123", client=_client(handler))

    assert seen_cookies == ["session=abc123"]


def test_fetch_library_raises_when_redirected_to_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return _sign_in_redirect_response(request)

    with pytest.raises(booth_library_service.BoothSessionExpiredError):
        booth_library_service.fetch_library("expired=1", client=_client(handler))


def test_fetch_library_returns_empty_list_when_robots_disallows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(booth_library_service, "robots_allow", lambda url: False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not fetch when robots.txt disallows")

    assert booth_library_service.fetch_library("session=abc", client=_client(handler)) == []


def test_sync_library_replaces_table_contents(app_db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        if page == "1":
            html = _library_page(
                [_library_card("111", "First", "Shop A", "shopa", ["a1.zip", "a2.zip"])]
            )
        else:
            html = _library_page([])
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    count = booth_library_service.sync_library(app_db_session, "session=abc", client=_client(handler))

    assert count == 2
    assert booth_library_service.library_size(app_db_session) == 2
    match = booth_library_service.find_by_filename(app_db_session, "a1.zip")
    assert match is not None
    assert match.product_url == "https://booth.pm/ja/items/111"
    assert match.product_name == "First"


def test_sync_library_overwrites_stale_rows_from_a_previous_sync(
    app_db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_robots(monkeypatch)
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        if page != "1":
            return httpx.Response(200, text=_library_page([]), headers={"content-type": "text/html"})
        call_count["n"] += 1
        name = "First" if call_count["n"] == 1 else "First Renamed"
        html = _library_page([_library_card("111", name, "Shop A", "shopa", ["a1.zip"])])
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    booth_library_service.sync_library(app_db_session, "session=abc", client=_client(handler))
    booth_library_service.sync_library(app_db_session, "session=abc", client=_client(handler))

    assert booth_library_service.library_size(app_db_session) == 1
    match = booth_library_service.find_by_filename(app_db_session, "a1.zip")
    assert match is not None
    assert match.product_name == "First Renamed"


def test_sync_library_leaves_table_untouched_when_session_expired(
    app_db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_robots(monkeypatch)

    def seed_handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        if page == "1":
            html = _library_page([_library_card("111", "First", "Shop A", "shopa", ["a1.zip"])])
        else:
            html = _library_page([])
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    booth_library_service.sync_library(app_db_session, "session=abc", client=_client(seed_handler))
    assert booth_library_service.library_size(app_db_session) == 1

    with pytest.raises(booth_library_service.BoothSessionExpiredError):
        booth_library_service.sync_library(app_db_session, "expired=1", client=_client(_sign_in_redirect_response))

    assert booth_library_service.library_size(app_db_session) == 1


def test_find_by_filename_returns_none_when_no_match(app_db_session: Session) -> None:
    assert booth_library_service.find_by_filename(app_db_session, "does_not_exist.zip") is None
