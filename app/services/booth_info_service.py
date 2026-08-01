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
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

from app.services.booth_common import REQUEST_TIMEOUT_SECONDS, USER_AGENT, robots_allow

logger = logging.getLogger(__name__)


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

        with httpx.Client(
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as owned_client:
            return _fetch(owned_client, product_url)
    except Exception:
        logger.warning("product info auto-fetch failed for %s", product_url, exc_info=True)
        return None
