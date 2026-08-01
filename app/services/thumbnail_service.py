"""Best-effort thumbnail auto-fetch from a BOOTH product page's og:image.

Kept deliberately low-risk relative to the "no BOOTH automation" policy:
robots.txt is honored, exactly one HTML page is fetched (no crawling/link
following beyond the single og:image resource), and any failure at any step
(network, missing tag, non-image response, oversized) is swallowed and
logged as a warning -- callers always have a manual-upload fallback, so
auto-fetch failing must never block item registration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urljoin

import filetype
import httpx
from bs4 import BeautifulSoup

from app.services.booth_common import REQUEST_TIMEOUT_SECONDS, USER_AGENT
from app.services.booth_common import robots_allow as _robots_allow

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024


@dataclass(slots=True)
class FetchedThumbnail:
    content: bytes
    content_type: str


def _fetch(client: httpx.Client, product_url: str) -> FetchedThumbnail | None:
    page_response = client.get(product_url)
    page_response.raise_for_status()
    if "text/html" not in page_response.headers.get("content-type", ""):
        return None

    soup = BeautifulSoup(page_response.text, "html.parser")
    og_image = soup.find("meta", attrs={"property": "og:image"})
    image_url = og_image.get("content") if og_image else None
    if not image_url:
        logger.info("no og:image found at %s", product_url)
        return None
    image_url = urljoin(str(page_response.url), image_url)

    image_response = client.get(image_url)
    image_response.raise_for_status()
    content_type = image_response.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        return None
    content = image_response.content
    if len(content) > MAX_IMAGE_BYTES or len(content) == 0:
        return None

    kind = filetype.guess(content)
    if kind is None or not kind.mime.startswith("image/"):
        logger.warning("fetched thumbnail content did not verify as an image (%s)", image_url)
        return None

    return FetchedThumbnail(content=content, content_type=kind.mime)


def try_fetch_thumbnail(product_url: str | None, *, client: httpx.Client | None = None) -> FetchedThumbnail | None:
    """`client` is injectable for tests (httpx.MockTransport); production callers omit it."""
    if not product_url:
        return None

    try:
        if not _robots_allow(product_url):
            logger.info("robots.txt disallows fetching %s; skipping auto thumbnail", product_url)
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
        logger.warning("thumbnail auto-fetch failed for %s", product_url, exc_info=True)
        return None
