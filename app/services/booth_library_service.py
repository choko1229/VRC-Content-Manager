"""Cross-references an uploaded file's original filename against the user's
own BOOTH purchase library (accounts.booth.pm/library and .../free_downloads),
to suggest a BoothURL with much higher confidence than the public keyword
search (booth_info_service.search_products) can offer: an exact filename
match means the file is *known* to have come from that exact product, not
just plausibly related to it.

The two listings have different markup: purchased-item cards list every
file inline, but free-download cards don't -- BOOTH only prints those on
the product's own page (one per downloadable "variation"; a product can
have several, and some may be paid variations sitting alongside the free
one with no filename at all). So syncing free downloads costs one extra
page fetch per product on top of the listing pages themselves -- still
within this module's "authenticated, user-triggered only" fetch policy
(see below), just a heavier version of it.

This requires an authenticated request (the library page is login-gated),
so unlike the rest of the booth_* services this one needs a session cookie
the user extracts from their own browser and pastes into Settings -- see
app/core/instance_config.py's booth_library_cookie field for why that's kept
out of the Drive-synced database. The cookie is used only to fetch this one
page, once per explicit "sync" click (never automatically/on a schedule) --
see app/web/fragments/settings.py.

The library listing is re-fetched wholesale into booth_library_files on
every sync (delete + reinsert) rather than diffed incrementally, since BOOTH
is always the source of truth and there's nothing meaningful to preserve
between syncs. That table has no secrets in it (just the user's own product
names/filenames, already visible to them), so -- unlike the cookie -- it's
fine that it lives in the regular Drive-synced database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.session import db_write_lock
from app.models.booth_library_file import BoothLibraryFile
from app.services import app_settings_service
from app.services.booth_common import make_client, robots_allow

logger = logging.getLogger(__name__)

_MAX_PAGES = 200  # sanity cap so a parsing regression can't loop forever
_SETTING_LAST_SYNCED_AT = "booth_library_synced_at"


class BoothSessionExpiredError(Exception):
    """The stored cookie no longer authenticates -- BOOTH redirected to its
    login page instead of returning the library. The user needs to grab a
    fresh cookie from their browser and re-save it in Settings."""


@dataclass(slots=True)
class BoothLibraryEntry:
    product_url: str
    product_name: str
    shop_name: str | None
    shop_url: str | None
    thumbnail_url: str | None
    filenames: list[str]


def _parse_page(html: str) -> list[BoothLibraryEntry]:
    soup = BeautifulSoup(html, "html.parser")
    entries: list[BoothLibraryEntry] = []
    for thumb in soup.find_all("img", class_="l-library-item-thumbnail"):
        thumb_link = thumb.find_parent("a")
        product_url = thumb_link.get("href") if thumb_link else None
        card = thumb.find_parent(
            "div", class_=lambda c: bool(c) and "p-16" in c.split() and "bg-white" in c.split()
        )
        if card is None or not product_url:
            continue

        name_div = card.select_one("div.text-16")
        product_name = name_div.get_text(strip=True) if name_div else None
        if not product_name:
            continue

        shop_name_div = card.select_one("div.text-text-gray600")
        shop_name = shop_name_div.get_text(strip=True) if shop_name_div else None
        shop_link = shop_name_div.find_parent("a") if shop_name_div else None
        shop_url = shop_link.get("href") if shop_link else None

        filenames = [
            d.get_text(strip=True)
            for d in card.select("div.min-w-0.break-words.whitespace-pre-line > div.text-14")
        ]

        entries.append(
            BoothLibraryEntry(
                product_url=product_url,
                product_name=product_name,
                shop_name=shop_name,
                shop_url=shop_url,
                thumbnail_url=thumb.get("src"),
                filenames=filenames,
            )
        )
    return entries


# Free-download listing cards carry no filename of their own -- see the
# module docstring -- so BoothLibraryEntry.filenames starts empty for them
# and gets backfilled from the product's own page via this selector. Not
# every .variation-cart on that page has one: a product can offer several
# variations (colors, sizes, ...), only some of which are the free one this
# library entry refers to; the rest (paid variations) have no filename div
# at all and are skipped naturally.
_PRODUCT_PAGE_FILENAME_SELECTOR = "div.text-14.text-text-default.font-normal.text-left"


def _parse_product_page_filenames(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    filenames = []
    for cart in soup.select(".variation-cart"):
        el = cart.select_one(_PRODUCT_PAGE_FILENAME_SELECTOR)
        text = el.get_text(strip=True) if el else None
        if text:
            filenames.append(text)
    return filenames


def _fetch_product_filenames(client: httpx.Client, cookie: str, product_url: str) -> list[str]:
    if not robots_allow(product_url, client=client):
        logger.info("robots.txt disallows fetching %s; skipping filename lookup for this free download", product_url)
        return []
    try:
        response = client.get(product_url, headers={"Cookie": cookie})
        response.raise_for_status()
    except httpx.HTTPError:
        logger.warning("booth_library_service: failed to fetch free-download product page %s", product_url, exc_info=True)
        return []
    return _parse_product_page_filenames(response.text)


def _paginate(
    client: httpx.Client, cookie: str, base_url: str, *, backfill_filenames: bool
) -> list[BoothLibraryEntry]:
    """Shared pagination loop for both listings: pages until one comes back
    with no product cards (confirmed: BOOTH returns 200 with an empty list
    past the last page rather than erroring), raising
    BoothSessionExpiredError the moment a page redirects to the login page
    instead. `backfill_filenames` is only set for the free-downloads
    listing -- see the module docstring for why."""
    all_entries: list[BoothLibraryEntry] = []
    for page in range(1, _MAX_PAGES + 1):
        response = client.get(f"{base_url}?page={page}", headers={"Cookie": cookie})
        response.raise_for_status()
        if response.url.path.startswith("/users/sign_in"):
            raise BoothSessionExpiredError
        page_entries = _parse_page(response.text)
        if not page_entries:
            break
        if backfill_filenames:
            for entry in page_entries:
                entry.filenames = _fetch_product_filenames(client, cookie, entry.product_url)
        all_entries.extend(page_entries)
    return all_entries


def fetch_library(cookie: str, *, client: httpx.Client | None = None) -> list[BoothLibraryEntry]:
    """Paginates accounts.booth.pm/library?page=1,2,... (purchased items --
    each card already lists every file inline). `client` is injectable for
    tests (httpx.MockTransport); production callers omit it."""
    if not robots_allow("https://accounts.booth.pm/library"):
        logger.info("robots.txt disallows fetching accounts.booth.pm/library; skipping library sync")
        return []

    def _run(active_client: httpx.Client) -> list[BoothLibraryEntry]:
        return _paginate(active_client, cookie, "https://accounts.booth.pm/library", backfill_filenames=False)

    if client is not None:
        return _run(client)
    with make_client() as owned_client:
        return _run(owned_client)


def fetch_free_downloads(cookie: str, *, client: httpx.Client | None = None) -> list[BoothLibraryEntry]:
    """Paginates accounts.booth.pm/library/free_downloads?page=1,2,...,
    fetching each product's own page to fill in filenames (see the module
    docstring). A single product page fetch failing just leaves that one
    entry with no filenames -- best-effort, matching the rest of this
    module -- rather than failing the whole sync. `client` is injectable
    for tests; production callers omit it."""
    if not robots_allow("https://accounts.booth.pm/library/free_downloads"):
        logger.info("robots.txt disallows fetching accounts.booth.pm/library/free_downloads; skipping")
        return []

    def _run(active_client: httpx.Client) -> list[BoothLibraryEntry]:
        return _paginate(
            active_client, cookie, "https://accounts.booth.pm/library/free_downloads", backfill_filenames=True
        )

    if client is not None:
        return _run(client)
    with make_client() as owned_client:
        return _run(owned_client)


def sync_library(db: Session, cookie: str, *, client: httpx.Client | None = None) -> int:
    """Fetches both the purchased-items and free-downloads libraries and
    replaces booth_library_files wholesale. Returns the number of (product,
    filename) rows stored. Raises BoothSessionExpiredError if the cookie no
    longer authenticates -- the table is left untouched in that case
    (nothing is deleted until both fetches have actually succeeded)."""
    entries = fetch_library(cookie, client=client) + fetch_free_downloads(cookie, client=client)

    with db_write_lock:
        db.execute(delete(BoothLibraryFile))
        count = 0
        for entry in entries:
            for filename in entry.filenames:
                db.add(
                    BoothLibraryFile(
                        filename=filename,
                        product_url=entry.product_url,
                        product_name=entry.product_name,
                        shop_name=entry.shop_name,
                        shop_url=entry.shop_url,
                        thumbnail_url=entry.thumbnail_url,
                    )
                )
                count += 1
        # set_setting commits internally, which also flushes the inserts/
        # delete above -- no separate db.commit() needed.
        app_settings_service.set_setting(db, _SETTING_LAST_SYNCED_AT, datetime.now(timezone.utc).isoformat())
    logger.info("booth_library_service: synced %s files across %s products", count, len(entries))
    return count


def find_by_filename(db: Session, filename: str) -> BoothLibraryFile | None:
    return db.execute(select(BoothLibraryFile).where(BoothLibraryFile.filename == filename)).scalars().first()


def library_size(db: Session) -> int:
    return len(db.execute(select(BoothLibraryFile.id)).scalars().all())
