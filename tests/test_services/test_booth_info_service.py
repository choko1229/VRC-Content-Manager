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


HTML_SHOP_PAGE = (
    '<html><head><meta property="og:title" content="SheepySnow">'
    '<meta property="og:image" content="https://booth.pximg.net/shop-icon.jpg">'
    '<meta property="og:description" content="3Dアバターとオリジナル衣装のショップです。"></head></html>'
)

HTML_SHOP_PAGE_WITHOUT_METADATA = "<html><head></head></html>"


def test_try_fetch_shop_info_reads_ogp_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=HTML_SHOP_PAGE, headers={"content-type": "text/html"})

    result = booth_info_service.try_fetch_shop_info("https://sheepysnow.booth.pm/", client=_client(handler))

    assert result is not None
    assert result.name == "SheepySnow"
    assert result.icon_url == "https://booth.pximg.net/shop-icon.jpg"
    assert result.description == "3Dアバターとオリジナル衣装のショップです。"


def test_try_fetch_shop_info_returns_none_without_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=HTML_SHOP_PAGE_WITHOUT_METADATA, headers={"content-type": "text/html"})

    result = booth_info_service.try_fetch_shop_info("https://sheepysnow.booth.pm/", client=_client(handler))

    assert result is None


def test_try_fetch_shop_info_returns_none_when_robots_disallows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(booth_info_service, "robots_allow", lambda url: False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not fetch when robots.txt disallows")

    result = booth_info_service.try_fetch_shop_info("https://sheepysnow.booth.pm/", client=_client(handler))

    assert result is None


def test_try_fetch_shop_info_returns_none_for_empty_url() -> None:
    assert booth_info_service.try_fetch_shop_info(None) is None
    assert booth_info_service.try_fetch_shop_info("") is None


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


def _search_card(product_id: str, name: str, shop_name: str, thumbnail: str) -> str:
    # Trimmed-down version of BOOTH's real search-result markup: the fields
    # we read all come from data-* attributes and two class-named children.
    return (
        f'<li class="item-card l-card" data-product-id="{product_id}" data-product-name="{name}">'
        f'<a class="item-card__thumbnail-image" href="https://booth.pm/ja/items/{product_id}" '
        f'data-original="{thumbnail}"></a>'
        f'<div class="item-card__shop-info"><a class="item-card__shop-name-anchor">'
        f'<div class="item-card__shop-name">{shop_name}</div></a></div>'
        "</li>"
    )


HTML_SEARCH_RESULTS = (
    '<html><body><ul class="l-cards-5cols">'
    + _search_card("2280136", "VirtualLens2", "ろじらぼ", "https://booth.pximg.net/thumb-a.jpg")
    + _search_card("4835743", "やさしいくま", "SheepySnow", "https://booth.pximg.net/thumb-b.jpg")
    + "</ul></body></html>"
)

HTML_SEARCH_NO_RESULTS = '<html><body><ul class="l-cards-5cols"></ul></body></html>'


def test_search_products_parses_result_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=HTML_SEARCH_RESULTS, headers={"content-type": "text/html"})

    results = booth_info_service.search_products("アバター", client=_client(handler))

    assert len(results) == 2
    assert results[0].product_url == "https://booth.pm/ja/items/2280136"
    assert results[0].name == "VirtualLens2"
    assert results[0].shop_name == "ろじらぼ"
    assert results[0].thumbnail_url == "https://booth.pximg.net/thumb-a.jpg"


def test_search_products_returns_empty_list_for_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=HTML_SEARCH_NO_RESULTS, headers={"content-type": "text/html"})

    assert booth_info_service.search_products("該当なし", client=_client(handler)) == []


def test_search_products_returns_empty_list_for_empty_query() -> None:
    assert booth_info_service.search_products("") == []
    assert booth_info_service.search_products(None) == []


def test_search_products_returns_empty_list_when_robots_disallows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(booth_info_service, "robots_allow", lambda url: False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not fetch when robots.txt disallows")

    assert booth_info_service.search_products("アバター", client=_client(handler)) == []


def test_search_products_returns_empty_list_on_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    assert booth_info_service.search_products("アバター", client=_client(handler)) == []


def test_search_products_caps_results_at_five(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_robots(monkeypatch)
    many_cards = "".join(
        _search_card(str(i), f"item{i}", "shop", "https://booth.pximg.net/t.jpg") for i in range(10)
    )
    html = f'<html><body><ul class="l-cards-5cols">{many_cards}</ul></body></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    results = booth_info_service.search_products("アバター", client=_client(handler))

    assert len(results) == 5
