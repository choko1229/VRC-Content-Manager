"""Shared fetch policy for read-only product-page scraping (thumbnail + metadata).

Both auto-fetch features pull from the same single product page under the
same low-risk policy: robots.txt honored, exactly one page fetched (no
crawling/link following beyond the one og:image/JSON-LD resource), short
timeout, dedicated UA string. Kept in one place so the two callers can't
drift apart.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

logger = logging.getLogger(__name__)

USER_AGENT = "BOOTHAssetManagerBot/0.1 (personal single-user asset tracker; metadata/thumbnail fetch only)"
REQUEST_TIMEOUT_SECONDS = 8.0


def robots_allow(url: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        parser.read()
    except OSError:
        logger.warning("could not read robots.txt at %s; skipping auto fetch", robots_url)
        return False
    return parser.can_fetch(USER_AGENT, url)
