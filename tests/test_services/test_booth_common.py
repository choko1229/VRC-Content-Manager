from __future__ import annotations

from typing import Callable

import httpx

from app.services import booth_common


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_robots_allow_true_when_path_not_disallowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="User-agent: *\nDisallow: /terms\n")

    assert booth_common.robots_allow("https://booth.pm/ja/items/123", client=_client(handler)) is True


def test_robots_allow_false_when_path_disallowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="User-agent: *\nDisallow: /ja/items\n")

    assert booth_common.robots_allow("https://booth.pm/ja/items/123", client=_client(handler)) is False


def test_make_client_sends_our_user_agent_not_default_urllib_ua() -> None:
    # The actual bug: RobotFileParser.read()'s own urllib call sends no
    # User-Agent of its own, and BOOTH's Cloudflare bot protection 403s that
    # specific default UA -- this pins down that the client robots_allow
    # falls back to when no client is injected carries our USER_AGENT
    # instead, which is what avoids that block in production.
    client = booth_common.make_client()
    try:
        assert client.headers["user-agent"] == booth_common.USER_AGENT
    finally:
        client.close()


def test_robots_allow_false_on_403_treats_whole_site_as_disallowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    assert booth_common.robots_allow("https://booth.pm/ja/items/123", client=_client(handler)) is False


def test_robots_allow_true_on_404_treats_site_as_unrestricted() -> None:
    # Matches RobotFileParser.read()'s own convention: a 4xx other than
    # 401/403 (e.g. no robots.txt at all) means "nothing to restrict us".
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    assert booth_common.robots_allow("https://booth.pm/ja/items/123", client=_client(handler)) is True


def test_robots_allow_false_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    assert booth_common.robots_allow("https://booth.pm/ja/items/123", client=_client(handler)) is False
