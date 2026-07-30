from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from app.services import thumbnail_service

HTML_WITH_OG_IMAGE = '<html><head><meta property="og:image" content="https://example.com/thumb.jpg"></head></html>'
HTML_WITHOUT_OG_IMAGE = "<html><head></head></html>"
JPG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _allow_robots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests here exercise the HTML/image fetch logic, not robots.txt parsing
    (which is stdlib behavior, not ours, and is covered separately below) --
    call this explicitly to stub it as always-allowed."""
    monkeypatch.setattr(thumbnail_service, "_robots_allow", lambda url: True)


def test_try_fetch_thumbnail_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://booth.example/items/1":
            return httpx.Response(200, text=HTML_WITH_OG_IMAGE, headers={"content-type": "text/html"})
        if str(request.url) == "https://example.com/thumb.jpg":
            return httpx.Response(200, content=JPG_BYTES, headers={"content-type": "image/jpeg"})
        raise AssertionError(f"unexpected request: {request.url}")

    result = thumbnail_service.try_fetch_thumbnail("https://booth.example/items/1", client=_client(handler))

    assert result is not None
    assert result.content == JPG_BYTES
    assert result.content_type.startswith("image/")


def test_try_fetch_thumbnail_returns_none_when_no_og_image(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=HTML_WITHOUT_OG_IMAGE, headers={"content-type": "text/html"})

    result = thumbnail_service.try_fetch_thumbnail("https://booth.example/items/2", client=_client(handler))

    assert result is None


def test_try_fetch_thumbnail_returns_none_for_non_html_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{}", headers={"content-type": "application/json"})

    result = thumbnail_service.try_fetch_thumbnail("https://booth.example/items/3", client=_client(handler))

    assert result is None


def test_try_fetch_thumbnail_returns_none_when_image_fetch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://booth.example/items/4":
            return httpx.Response(200, text=HTML_WITH_OG_IMAGE, headers={"content-type": "text/html"})
        return httpx.Response(404)

    result = thumbnail_service.try_fetch_thumbnail("https://booth.example/items/4", client=_client(handler))

    assert result is None


def test_try_fetch_thumbnail_returns_none_when_robots_disallows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(thumbnail_service, "_robots_allow", lambda url: False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not fetch when robots.txt disallows")

    result = thumbnail_service.try_fetch_thumbnail("https://booth.example/items/5", client=_client(handler))

    assert result is None


def test_try_fetch_thumbnail_returns_none_for_empty_url() -> None:
    assert thumbnail_service.try_fetch_thumbnail(None) is None
    assert thumbnail_service.try_fetch_thumbnail("") is None


def test_robots_allow_fails_closed_on_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.robotparser import RobotFileParser

    def _raise_read(self) -> None:
        raise OSError("network unreachable")

    monkeypatch.setattr(RobotFileParser, "read", _raise_read)

    assert thumbnail_service._robots_allow("https://booth.example/items/6") is False
