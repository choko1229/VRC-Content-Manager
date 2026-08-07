"""Best-effort product metadata auto-fetch, to prefill the ingest form.

Same low-risk policy as thumbnail_service (see booth_common): robots.txt
honored, exactly one page fetched, all failures swallowed and logged. The
result only ever prefills form fields the user reviews and can still edit
before submitting, so a fetch failure must never block manual entry.

BOOTH product pages carry a schema.org Product JSON-LD block with
structured name/price/brand/image, which is far more reliable than parsing
og:title's "<name> - <shop> - BOOTH" convention -- so JSON-LD is tried
first, with the og:title/og:image heuristic as a fallback for pages that
lack it.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from app.services.booth_common import make_client, robots_allow

logger = logging.getLogger(__name__)

_MAX_SEARCH_RESULTS = 5

# Item titles auto-derived from an uploaded filename (see
# app/web/pages/items.py:_derive_name_from_filename) keep the filename's own
# word separators (e.g. "grade_hair_2.3"), which BOOTH's search treats as
# literal characters rather than word breaks -- collapsing them to spaces
# before searching makes the query look like something a human would type.
_QUERY_SEPARATOR_RE = re.compile(r"[_.]+")
_QUERY_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(slots=True)
class BoothProductInfo:
    name: str | None
    shop_name: str | None
    shop_url: str | None
    price: int | None
    image_url: str | None
    description: str | None


def _from_json_ld(soup: BeautifulSoup) -> BoothProductInfo | None:
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("@type") != "Product":
            continue

        offers = data.get("offers") or {}
        brand = data.get("brand") or {}
        price = None
        price_raw = offers.get("price") if isinstance(offers, dict) else None
        if price_raw is not None:
            try:
                price = int(float(price_raw))
            except (TypeError, ValueError):
                price = None

        description = data.get("description")
        return BoothProductInfo(
            name=data.get("name") or None,
            shop_name=brand.get("name") if isinstance(brand, dict) else None,
            shop_url=brand.get("url") if isinstance(brand, dict) else None,
            price=price,
            image_url=data.get("image") or None,
            description=description.strip() if isinstance(description, str) and description.strip() else None,
        )
    return None


def _from_meta_tags(soup: BeautifulSoup) -> BoothProductInfo | None:
    og_title = soup.find("meta", attrs={"property": "og:title"})
    og_image = soup.find("meta", attrs={"property": "og:image"})
    og_description = soup.find("meta", attrs={"property": "og:description"})
    title = (og_title.get("content") if og_title else None) or None
    if not title:
        return None

    # BOOTH's og:title convention: "<item name> - <shop name> - BOOTH"
    name, shop_name = title, None
    parts = title.rsplit(" - ", 2)
    if len(parts) == 3 and parts[2] == "BOOTH":
        name, shop_name = parts[0], parts[1]

    description = og_description.get("content") if og_description else None
    return BoothProductInfo(
        name=name or None,
        shop_name=shop_name,
        shop_url=None,
        price=None,
        image_url=(og_image.get("content") if og_image else None) or None,
        description=description.strip() if isinstance(description, str) and description.strip() else None,
    )


def _fetch(client: httpx.Client, product_url: str) -> BoothProductInfo | None:
    response = client.get(product_url)
    response.raise_for_status()
    if "text/html" not in response.headers.get("content-type", ""):
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    return _from_json_ld(soup) or _from_meta_tags(soup)


@dataclass(slots=True)
class BoothShopInfo:
    name: str | None
    icon_url: str | None
    description: str | None


def _shop_info_from_meta_tags(soup: BeautifulSoup) -> BoothShopInfo | None:
    # BOOTH shop pages don't carry a Product JSON-LD block (they're not a
    # single product), so this relies on the same generic OGP convention
    # _from_meta_tags uses as its product-page fallback.
    og_title = soup.find("meta", attrs={"property": "og:title"})
    og_image = soup.find("meta", attrs={"property": "og:image"})
    og_description = soup.find("meta", attrs={"property": "og:description"})
    name = (og_title.get("content") if og_title else None) or None
    icon_url = (og_image.get("content") if og_image else None) or None
    if not name and not icon_url:
        return None

    description = og_description.get("content") if og_description else None
    return BoothShopInfo(
        name=name,
        icon_url=icon_url,
        description=description.strip() if isinstance(description, str) and description.strip() else None,
    )


def _fetch_shop(client: httpx.Client, shop_url: str) -> BoothShopInfo | None:
    response = client.get(shop_url)
    response.raise_for_status()
    if "text/html" not in response.headers.get("content-type", ""):
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    return _shop_info_from_meta_tags(soup)


def try_fetch_shop_info(shop_url: str | None, *, client: httpx.Client | None = None) -> BoothShopInfo | None:
    """`client` is injectable for tests (httpx.MockTransport); production callers omit it."""
    if not shop_url:
        return None

    try:
        if not robots_allow(shop_url):
            logger.info("robots.txt disallows fetching %s; skipping auto shop-info fetch", shop_url)
            return None

        if client is not None:
            return _fetch_shop(client, shop_url)

        with make_client() as owned_client:
            return _fetch_shop(owned_client, shop_url)
    except Exception:
        logger.warning("shop info auto-fetch failed for %s", shop_url, exc_info=True)
        return None


def match_known_terms(known: list[str], *texts: str | None) -> list[str]:
    """Suggest already-known tag/avatar names that show up in the fetched name/description.

    Deliberately conservative: only ever resurfaces terms the user has already
    used before (never invents new ones), so a suggestion is always something
    they'd recognize -- the user still has to click to accept it.
    """
    haystack = " ".join(t for t in texts if t).lower()
    if not haystack:
        return []
    return [term for term in known if term and term.lower() in haystack]


def try_fetch_product_info(product_url: str | None, *, client: httpx.Client | None = None) -> BoothProductInfo | None:
    """`client` is injectable for tests (httpx.MockTransport); production callers omit it."""
    if not product_url:
        return None

    try:
        if not robots_allow(product_url):
            logger.info("robots.txt disallows fetching %s; skipping auto product-info fetch", product_url)
            return None

        if client is not None:
            return _fetch(client, product_url)

        with make_client() as owned_client:
            return _fetch(owned_client, product_url)
    except Exception:
        logger.warning("product info auto-fetch failed for %s", product_url, exc_info=True)
        return None


@dataclass(slots=True)
class BoothSearchResult:
    product_url: str
    name: str
    shop_name: str | None
    thumbnail_url: str | None


def _parse_search_results(soup: BeautifulSoup) -> list[BoothSearchResult]:
    results: list[BoothSearchResult] = []
    for card in soup.find_all("li", class_="item-card"):
        name = card.get("data-product-name")
        thumb_anchor = card.find("a", class_="item-card__thumbnail-image")
        product_url = thumb_anchor.get("href") if thumb_anchor else None
        if not name or not product_url:
            continue

        shop_name_el = card.find(class_="item-card__shop-name")
        results.append(
            BoothSearchResult(
                product_url=product_url,
                name=name,
                shop_name=shop_name_el.get_text(strip=True) if shop_name_el else None,
                thumbnail_url=thumb_anchor.get("data-original") if thumb_anchor else None,
            )
        )
        if len(results) >= _MAX_SEARCH_RESULTS:
            break
    return results


def _search(client: httpx.Client, search_url: str) -> list[BoothSearchResult]:
    response = client.get(search_url)
    response.raise_for_status()
    if "text/html" not in response.headers.get("content-type", ""):
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    return _parse_search_results(soup)


def search_products(query: str | None, *, client: httpx.Client | None = None) -> list[BoothSearchResult]:
    """Best-effort candidate search, used to suggest a likely BOOTH product
    page for an item whose name was only ever derived from an uploaded
    filename. Same low-risk policy as the rest of this module: one page
    fetched, all failures swallowed and logged -- results are only ever a
    clickable suggestion the user reviews and accepts (or ignores) before
    anything is filled in, so a bad or empty result must never block manual
    entry. `client` is injectable for tests (httpx.MockTransport);
    production callers omit it.
    """
    query = (query or "").strip()
    if not query:
        return []

    normalized_query = _QUERY_WHITESPACE_RE.sub(" ", _QUERY_SEPARATOR_RE.sub(" ", query)).strip()
    search_url = f"https://booth.pm/ja/search/{quote(normalized_query)}"
    try:
        if not robots_allow(search_url):
            logger.info("robots.txt disallows fetching %s; skipping BOOTH search suggestions", search_url)
            return []

        if client is not None:
            return _search(client, search_url)

        with make_client() as owned_client:
            return _search(owned_client, search_url)
    except Exception:
        logger.warning("BOOTH search suggestion fetch failed for query=%r", query, exc_info=True)
        return []
