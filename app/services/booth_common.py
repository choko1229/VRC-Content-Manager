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

import httpx
import truststore

logger = logging.getLogger(__name__)

USER_AGENT = "BOOTHAssetManagerBot/0.1 (personal single-user asset tracker; metadata/thumbnail fetch only)"
REQUEST_TIMEOUT_SECONDS = 8.0

# Patches ssl.SSLContext process-wide so verification goes through the
# platform's native trust store/APIs (SChannel on Windows, Security
# framework on macOS, OpenSSL against the system store on Linux) instead of
# only httpx/certifi's bundled public-root list. Certifi-only verification
# fails every request with "unable to get local issuer certificate" on a
# machine where outbound HTTPS is intercepted (corporate proxy, antivirus
# TLS scanning) even though the OS already trusts the intercepting
# certificate. Safe to call more than once; idempotent.
truststore.inject_into_ssl()


def make_client() -> httpx.Client:
    """A client for the two auto-fetch callers to use as their owned/default
    client (both also accept an injected `client` for tests). Verification
    goes through truststore -- see the inject_into_ssl() call above."""
    return httpx.Client(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )


def robots_allow(url: str, *, client: httpx.Client | None = None) -> bool:
    """Mirrors RobotFileParser.read()'s own status-code handling (401/403 ->
    treat the whole site as disallowed; other 4xx -> treat as unrestricted;
    else parse the body), but fetches through our own client instead of
    RobotFileParser.read()'s built-in urllib.request call -- that call sends
    no User-Agent of its own (bare "Python-urllib/x.y"), and BOOTH's
    Cloudflare bot protection 403s that specific default UA even though the
    exact same request succeeds immediately with our USER_AGENT string. That
    403 was silently misread as "robots.txt disallows everything", which is
    why auto-fetch failed identically for every BOOTH URL regardless of
    which of the two URL formats (shop subdomain vs. booth.pm/<locale>) was
    used -- it was never about the URL shape.

    `client` is injectable for tests (httpx.MockTransport); production
    callers omit it.
    """
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        if client is not None:
            response = client.get(robots_url)
        else:
            with make_client() as owned_client:
                response = owned_client.get(robots_url)
    except httpx.HTTPError:
        logger.warning("could not read robots.txt at %s; skipping auto fetch", robots_url)
        return False

    if response.status_code in (401, 403):
        parser.disallow_all = True
    elif response.status_code >= 400:
        parser.allow_all = True
    else:
        parser.parse(response.text.splitlines())
    return parser.can_fetch(USER_AGENT, url)
