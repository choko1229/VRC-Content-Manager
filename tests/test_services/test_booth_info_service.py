from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from app.services import booth_info_service

HTML_WITH_JSON_LD = """
<html><head>
<meta property="og:title" content="やさしいくま【VRChat想定3Dアバター】 - SheepySnow - BOOTH">
<meta property="og:image" content="https://booth.pximg.net/thumb.jpg">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"やさしいくま【VRChat想定3Dアバター】",
"description":"VRChat想定のHumanoid対応オリジナル3Dアバターです。マヌカ、桔梗に対応。",
"offers":{"priceCurrency":"JPY","@type":"Offer","price":"300"},
"brand":{"@type":"Brand","name":"SheepySnow","url":"https://watayukihii.booth.pm/"},
"image":"https://booth.pximg.net/thumb.jpg"}
</script>
</head></html>
"""

HTML_WITH_OG_TITLE_ONLY = (
    '<html><head><meta property="og:title" content="くまさん - SheepySnow - BOOTH">'
    '<meta property="og:image" content="https://booth.pximg.net/thumb2.jpg">'
    '<meta property="og:description" content="やさしい質感のテディベアです。"></head></html>'
)

HTML_WITHOUT_METADATA = "<html><head></head></html>"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _allow_robots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(booth_info_service, "robots_allow", lambda url: True)


def test_try_fetch_product_info_prefers_json_ld(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=HTML_WITH_JSON_LD, headers={"content-type": "text/html"})

    result = booth_info_service.try_fetch_product_info("https://booth.example/items/1", client=_client(handler))

    assert result is not None
    assert result.name == "やさしいくま【VRChat想定3Dアバター】"
    assert result.shop_name == "SheepySnow"
    assert result.shop_url == "https://watayukihii.booth.pm/"
    assert result.price == 300
    assert result.image_url == "https://booth.pximg.net/thumb.jpg"
    assert result.description == "VRChat想定のHumanoid対応オリジナル3Dアバターです。マヌカ、桔梗に対応。"


def test_try_fetch_product_info_falls_back_to_meta_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=HTML_WITH_OG_TITLE_ONLY, headers={"content-type": "text/html"})

    result = booth_info_service.try_fetch_product_info("https://booth.example/items/2", client=_client(handler))

    assert result is not None
    assert result.name == "くまさん"
    assert result.shop_name == "SheepySnow"
    assert result.price is None
    assert result.image_url == "https://booth.pximg.net/thumb2.jpg"
    assert result.description == "やさしい質感のテディベアです。"


def test_try_fetch_product_info_returns_none_without_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=HTML_WITHOUT_METADATA, headers={"content-type": "text/html"})

    result = booth_info_service.try_fetch_product_info("https://booth.example/items/3", client=_client(handler))

    assert result is None


def test_try_fetch_product_info_returns_none_for_non_html_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{}", headers={"content-type": "application/json"})

    result = booth_info_service.try_fetch_product_info("https://booth.example/items/4", client=_client(handler))

    assert result is None


def test_try_fetch_product_info_returns_none_when_robots_disallows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(booth_info_service, "robots_allow", lambda url: False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not fetch when robots.txt disallows")

    result = booth_info_service.try_fetch_product_info("https://booth.example/items/5", client=_client(handler))

    assert result is None


def test_try_fetch_product_info_returns_none_for_empty_url() -> None:
    assert booth_info_service.try_fetch_product_info(None) is None
    assert booth_info_service.try_fetch_product_info("") is None


def test_try_fetch_product_info_ignores_malformed_json_ld(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)
    html = (
        '<html><head><script type="application/ld+json">not json</script>'
        '<meta property="og:title" content="くまさん - SheepySnow - BOOTH"></head></html>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    result = booth_info_service.try_fetch_product_info("https://booth.example/items/6", client=_client(handler))

    assert result is not None
    assert result.name == "くまさん"


def test_match_known_terms_finds_case_insensitive_substring_matches() -> None:
    known = ["VRChat想定", "マヌカ", "桔梗", "未使用タグ"]

    matches = booth_info_service.match_known_terms(
        known, "やさしいくま【vrchat想定3Dアバター】", "マヌカ、桔梗に対応。"
    )

    assert matches == ["VRChat想定", "マヌカ", "桔梗"]


def test_match_known_terms_returns_empty_for_no_text() -> None:
    assert booth_info_service.match_known_terms(["タグ"], None, None) == []


def test_match_known_terms_returns_empty_when_nothing_matches() -> None:
    assert booth_info_service.match_known_terms(["無関係タグ"], "やさしいくま", "説明文") == []
