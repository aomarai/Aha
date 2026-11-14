"""Tests for the SSRF-safe fetcher: address policy, URL validation, resolver, and fetching."""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from aha.i18n import t
from aha.net import (
    DEFAULT_POLICY,
    USER_AGENT,
    BlockedAddressError,
    FetchError,
    FetchPolicy,
    Preview,
    SafeResolver,
    TTLCache,
    address_allowed,
    fetch_preview,
    parse_preview,
    validate_url,
)
from tests.helpers import FakeClock

# --------------------------------------------------------------------------- address policy


@pytest.mark.parametrize(
    "address",
    ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700::1111", "::ffff:8.8.8.8"],
)
def test_public_addresses_are_allowed(address: str) -> None:
    assert address_allowed(ipaddress.ip_address(address)) is True


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.255.255.254",
        "10.0.0.1",
        "10.43.0.1",  # the k3s API service
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",
        "224.0.0.1",  # multicast
        "239.255.255.250",
        "240.0.0.1",  # reserved
        "255.255.255.255",
        "192.0.0.1",
        "198.18.0.1",
        "192.0.2.1",  # documentation
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fec0::1",  # deprecated site-local, which is_global wrongly allows
        "ff02::1",  # multicast
        "::ffff:10.0.0.1",  # IPv4-mapped private
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "2001:db8::1",
        "2002:0a00:0001::1",  # 6to4 wrapping 10.0.0.1
        "64:ff9b::a00:1",  # NAT64 wrapping 10.0.0.1
    ],
)
def test_internal_and_special_addresses_are_refused(address: str) -> None:
    assert address_allowed(ipaddress.ip_address(address)) is False


# ----------------------------------------------------------------------------- URL validation


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/path?q=1", "https://example.com/path?q=1"),
        ("  https://example.com  ", "https://example.com"),
        ("HTTP://Example.COM/Path", "http://example.com/Path"),
        ("https://example.com:443/", "https://example.com/"),  # the default port is dropped
        ("http://example.com:80", "http://example.com"),
        ("http://8.8.8.8/", "http://8.8.8.8/"),
        ("https://bücher.example/", "https://xn--bcher-kva.example/"),
        ("https://[2606:4700::1111]/", "https://[2606:4700::1111]/"),
    ],
)
def test_valid_links_are_accepted_and_normalised(raw: str, expected: str) -> None:
    assert str(validate_url(raw)) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "That doesn't look like a valid link."),
        ("   ", "That doesn't look like a valid link."),
        ("https://exa mple.com", "That doesn't look like a valid link."),
        ("https://example.com/\nHost: evil", "That doesn't look like a valid link."),
        ("https://example.com/" + "a" * 2048, "That doesn't look like a valid link."),
        ("http://example.com:abc/", "That doesn't look like a valid link."),
        ("example.com", "Only http:// and https:// links are supported."),
        ("//example.com", "Only http:// and https:// links are supported."),
        ("ftp://example.com/", "Only http:// and https:// links are supported."),
        ("file:///etc/passwd", "Only http:// and https:// links are supported."),
        ("javascript:alert(1)", "Only http:// and https:// links are supported."),
        ("http:///path", "Only http:// and https:// links are supported."),
        ("http://user:pw@example.com/", "Links with a username or password are not supported."),
        ("http://user@example.com/", "Links with a username or password are not supported."),
        ("http://example.com:8080/", "Only the standard web ports (80 and 443) are supported."),
        ("http://example.com:22/", "Only the standard web ports (80 and 443) are supported."),
        ("http://127.0.0.1/", "That address isn't allowed."),
        ("http://10.0.0.1/", "That address isn't allowed."),
        ("http://169.254.169.254/latest/meta-data/", "That address isn't allowed."),
        ("http://0.0.0.0/", "That address isn't allowed."),
        ("http://[::1]/", "That address isn't allowed."),
        ("http://[::ffff:10.0.0.1]/", "That address isn't allowed."),
        ("http://[fe80::1]/", "That address isn't allowed."),
    ],
)
def test_unsafe_or_malformed_links_are_refused_with_a_reason(raw: str, message: str) -> None:
    with pytest.raises(FetchError) as caught:
        validate_url(raw)

    assert _catalogued(caught.value) == message


def test_a_link_of_exactly_the_length_limit_is_accepted() -> None:
    raw = "https://example.com/" + "a" * (2048 - len("https://example.com/"))

    assert len(raw) == 2048
    assert str(validate_url(raw)) == raw


@pytest.mark.parametrize("name", ["2130706433", "127.1", "0x7f.1", "0177.0.0.1", "localhost"])
def test_a_name_is_not_judged_until_it_is_resolved(name: str) -> None:
    # The operating system's resolver reads all of these as 127.0.0.1, but ``ipaddress`` does not,
    # so only resolution (see the next test) can tell.
    assert str(validate_url(f"http://{name}/")) == f"http://{name}/"


@pytest.mark.parametrize("name", ["2130706433", "127.1", "0x7f.1", "0177.0.0.1", "localhost"])
def test_the_real_resolver_never_returns_loopback_for_numeric_tricks(name: str) -> None:
    # Whatever this platform makes of the name, resolving it must fail rather than hand back an
    # address: a refusal (BlockedAddressError) or a failed lookup (socket.gaierror), both OSError.
    with pytest.raises(OSError):
        asyncio.run(SafeResolver().resolve(name, 80))


def test_a_policy_can_allow_other_ports() -> None:
    policy = FetchPolicy(ports=None)

    assert str(validate_url("http://example.com:8080/", policy)) == "http://example.com:8080/"


def test_the_production_policy_has_the_documented_limits() -> None:
    assert DEFAULT_POLICY.ports == frozenset({80, 443})
    assert DEFAULT_POLICY.max_bytes == 512 * 1024
    assert DEFAULT_POLICY.timeout == 5.0
    assert DEFAULT_POLICY.max_redirects == 3
    assert DEFAULT_POLICY.allowed is address_allowed


# --------------------------------------------------------------------------------- resolver


def _lookup(*addresses: str) -> Callable[..., Awaitable[list[Any]]]:
    async def lookup(host: str, port: int, **kwargs: Any) -> list[Any]:
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, port),
            )
            for address in addresses
        ]

    return lookup


def test_the_resolver_returns_public_addresses_pinned_for_connecting() -> None:
    resolver = SafeResolver(lookup=_lookup("93.184.216.34", "2606:2800:220:1::1"))

    results = asyncio.run(resolver.resolve("example.com", 443, socket.AF_UNSPEC))

    assert results == [
        {
            "hostname": "example.com",
            "host": "93.184.216.34",
            "port": 443,
            "family": socket.AF_INET,
            "proto": 6,
            "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
        },
        {
            "hostname": "example.com",
            "host": "2606:2800:220:1::1",
            "port": 443,
            "family": socket.AF_INET6,
            "proto": 6,
            "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
        },
    ]


@pytest.mark.parametrize("bad", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "fec0::1"])
def test_the_resolver_refuses_a_name_that_resolves_to_a_forbidden_address(bad: str) -> None:
    resolver = SafeResolver(lookup=_lookup(bad))

    with pytest.raises(BlockedAddressError, match="resolves to a forbidden address"):
        asyncio.run(resolver.resolve("2130706433", 80))


def test_one_forbidden_address_among_public_ones_refuses_the_whole_name() -> None:
    # An attacker's name may return a public and a private address, hoping the connector picks
    # the private one.
    resolver = SafeResolver(lookup=_lookup("93.184.216.34", "10.0.0.1"))

    with pytest.raises(BlockedAddressError):
        asyncio.run(resolver.resolve("evil.example", 80))


def test_the_resolver_strips_an_ipv6_scope_id() -> None:
    async def lookup(host: str, port: int, **kwargs: Any) -> list[Any]:
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111%eth0", port, 0, 2))]

    (result,) = asyncio.run(SafeResolver(lookup=lookup).resolve("example.com", 80))

    assert result["host"] == "2606:4700::1111"


def test_a_name_with_no_addresses_is_refused() -> None:
    with pytest.raises(BlockedAddressError, match="has no addresses"):
        asyncio.run(SafeResolver(lookup=_lookup()).resolve("example.com", 80))


def test_the_resolver_refuses_a_disallowed_port_before_looking_anything_up() -> None:
    called: list[str] = []

    async def lookup(host: str, port: int, **kwargs: Any) -> list[Any]:
        called.append(host)
        return []

    with pytest.raises(BlockedAddressError, match="port 8080 is not allowed"):
        asyncio.run(SafeResolver(lookup=lookup).resolve("example.com", 8080))

    assert called == []


def test_the_resolver_uses_the_event_loop_by_default_and_blocks_localhost() -> None:
    with pytest.raises(BlockedAddressError):
        asyncio.run(SafeResolver().resolve("localhost", 80))


def test_the_resolver_can_be_closed() -> None:
    asyncio.run(SafeResolver().close())


# ---------------------------------------------------------------------------- parse_preview

_PAGE = (
    "<!doctype html><html><head><title> Hello\n  World </title>"
    '<meta name="description" content="A  plain\ndescription">'
    '<meta property="og:title" content="OG Title">'
    '<meta property="og:description" content="OG description">'
    '<meta property="og:site_name" content="Example Site">'
    "</head><body><title>ignored</title></body></html>"
)


def test_open_graph_tags_win_over_the_plain_ones() -> None:
    assert parse_preview("https://x/", _PAGE) == Preview(
        url="https://x/",
        title="OG Title",
        description="OG description",
        site_name="Example Site",
    )


def test_the_title_and_description_tags_are_the_fallback_and_whitespace_is_squashed() -> None:
    page = (
        "<head><title> Hello\n  World </title>"
        '<meta name="description" content="A  plain\ndescription"></head>'
    )

    preview = parse_preview("https://x/", page)

    assert (preview.title, preview.description, preview.site_name) == (
        "Hello World",
        "A plain description",
        None,
    )


def test_html_entities_are_decoded() -> None:
    preview = parse_preview("https://x/", "<head><title>Tom &amp; Jerry &#8212; 2 &lt; 3</title>")

    assert preview.title == "Tom & Jerry — 2 < 3"


def test_nothing_after_the_head_is_read() -> None:
    preview = parse_preview("https://x/", "<head></head><title>late</title>")
    in_body = parse_preview("https://x/", "<body><title>late</title>")

    assert preview.title is None
    assert in_body.title is None


def test_the_first_title_wins() -> None:
    assert parse_preview("https://x/", "<head><title>one</title><title>two</title>").title == "one"


def test_empty_values_are_none() -> None:
    page = '<head><title>  </title><meta property="og:title" content=" "></head>'

    preview = parse_preview("https://x/", page)

    assert (preview.title, preview.description, preview.site_name) == (None, None, None)


def test_a_meta_tag_without_content_or_name_is_ignored() -> None:
    page = '<head><meta name="description"><meta content="orphan"><title>T</title></head>'

    preview = parse_preview("https://x/", page)

    assert (preview.title, preview.description) == ("T", None)


def test_meta_names_are_case_insensitive() -> None:
    page = '<head><meta name="Description" content="d"><meta property="OG:Title" content="o">'

    preview = parse_preview("https://x/", page)

    assert (preview.title, preview.description) == ("o", "d")


def test_the_first_of_two_identical_meta_tags_wins() -> None:
    page = '<head><meta name="description" content="first"><meta name="description" content="2nd">'

    assert parse_preview("https://x/", page).description == "first"


def test_a_truncated_or_garbage_document_does_not_raise() -> None:
    assert parse_preview("https://x/", "<head><title>cut off").title is None
    assert parse_preview("https://x/", "").title is None
    assert parse_preview("https://x/", "<<<>>> \x00 not html").title is None


# ------------------------------------------------------------------------------------ cache


def test_the_cache_returns_a_fresh_entry_and_forgets_an_old_one() -> None:
    clock = FakeClock(0.0)
    cache: TTLCache[str] = TTLCache(ttl=10.0, capacity=5, clock=clock)

    cache.put("a", "value")
    clock.now = 9.9
    assert cache.get("a") == "value"
    clock.now = 10.0  # exactly the time limit counts as expired
    assert cache.get("a") is None
    assert len(cache) == 0


def test_a_missing_key_is_none() -> None:
    assert TTLCache[str](ttl=1.0, capacity=1).get("nope") is None


def test_the_cache_evicts_the_oldest_entry_when_full() -> None:
    cache: TTLCache[int] = TTLCache(ttl=100.0, capacity=2, clock=FakeClock(0.0))

    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)

    assert (cache.get("a"), cache.get("b"), cache.get("c")) == (None, 2, 3)
    assert len(cache) == 2


def test_putting_an_existing_key_refreshes_it_without_evicting_others() -> None:
    clock = FakeClock(0.0)
    cache: TTLCache[int] = TTLCache(ttl=10.0, capacity=2, clock=clock)
    cache.put("a", 1)
    cache.put("b", 2)
    clock.now = 8.0

    cache.put("a", 10)  # refreshed: now the newest
    cache.put("c", 3)  # evicts the oldest ("b"), not "a"
    clock.now = 12.0

    assert cache.get("a") == 10
    assert cache.get("b") is None
    assert cache.get("c") == 3


# ------------------------------------------------------------------------------- fetching

_OPEN = FetchPolicy(allowed=lambda ip: True, ports=None, timeout=3.0)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class _Site:
    """A local web server whose handlers and requests the test controls."""

    def __init__(self, routes: dict[str, Handler]) -> None:
        self.requests: list[web.Request] = []
        self.server: TestServer | None = None
        self._routes = routes

    async def __aenter__(self) -> _Site:
        async def record(request: web.Request) -> web.StreamResponse:
            self.requests.append(request)
            return await self._routes[request.path](request)

        app = web.Application()
        for path in self._routes:
            app.router.add_get(path, record)
        self.server = TestServer(app)
        await self.server.start_server()
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.server is not None
        await self.server.close()

    def url(self, path: str = "/") -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.port}{path}"


def _html(body: str, **kwargs: Any) -> Handler:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(text=body, content_type="text/html", **kwargs)

    return handler


def _redirect(location: str, status: int = 302) -> Handler:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(status=status, headers={"Location": location})

    return handler


def _fetch(url: str, policy: FetchPolicy = _OPEN) -> Preview:
    return asyncio.run(fetch_preview(url, policy))


def _catalogued(error: FetchError) -> str:
    """The error's English text, after checking the catalogue says exactly the same.

    Every error a test provokes thereby proves its key exists in both languages and that the
    English in ``net.py`` and in ``en-US.json`` have not drifted apart.
    """
    assert t(error.key, "en-US", **error.values) == str(error)
    assert t(error.key, "de", **error.values) != str(error)
    return str(error)


def _fetch_error(url: str, policy: FetchPolicy = _OPEN) -> str:
    with pytest.raises(FetchError) as caught:
        asyncio.run(fetch_preview(url, policy))
    return _catalogued(caught.value)


def test_a_page_is_fetched_and_summarised() -> None:
    async def scenario() -> tuple[Preview, str]:
        async with _Site({"/": _html(_PAGE)}) as site:
            return await fetch_preview(site.url(), _OPEN), site.url()

    preview, url = asyncio.run(scenario())

    assert preview == Preview(url, "OG Title", "OG description", "Example Site")


def test_the_request_identifies_the_bot_and_carries_no_cookies() -> None:
    async def cookie_setter(request: web.Request) -> web.StreamResponse:
        response = web.Response(status=302, headers={"Location": "/page"})
        response.set_cookie("session", "secret")
        return response

    async def scenario() -> _Site:
        async with _Site({"/": cookie_setter, "/page": _html(_PAGE)}) as site:
            await fetch_preview(site.url(), _OPEN)
            return site

    site = asyncio.run(scenario())

    first, second = site.requests
    assert first.headers["User-Agent"] == USER_AGENT
    assert first.headers["Accept"] == "text/html"
    assert "Cookie" not in second.headers  # the jar discards cookies


def test_relative_and_absolute_redirects_are_followed() -> None:
    async def scenario() -> Preview:
        async with _Site(
            {"/": _redirect("/two", 301), "/two": _redirect("", 307), "/x": _html(_PAGE)}
        ) as site:
            site._routes["/two"] = _redirect(site.url("/x"), 307)
            return await fetch_preview(site.url(), _OPEN)

    preview = asyncio.run(scenario())

    assert preview.title == "OG Title"
    assert preview.url.endswith("/x")


def test_redirects_are_limited() -> None:
    async def scenario() -> tuple[str, int]:
        async with _Site({"/": _redirect("/")}) as site:
            try:
                await fetch_preview(site.url(), _OPEN)
            except FetchError as exc:
                return _catalogued(exc), len(site.requests)
            raise AssertionError("expected a FetchError")

    message, requests = asyncio.run(scenario())

    assert message == "The link redirects too many times."
    assert requests == 4  # the original request plus three redirects


def test_a_redirect_into_a_forbidden_address_is_refused_before_any_request_is_made() -> None:
    allow_only_loopback = FetchPolicy(
        allowed=lambda ip: str(ip) == "127.0.0.1", ports=None, timeout=3.0
    )

    async def scenario() -> tuple[str, int]:
        async with _Site({"/": _redirect("http://169.254.169.254/latest/meta-data/")}) as site:
            try:
                await fetch_preview(site.url(), allow_only_loopback)
            except FetchError as exc:
                return _catalogued(exc), len(site.requests)
            raise AssertionError("expected a FetchError")

    message, requests = asyncio.run(scenario())

    assert message == "That address isn't allowed."
    assert requests == 1  # only the first hop reached the server


def test_a_redirect_to_another_scheme_is_refused() -> None:
    async def scenario() -> str:
        async with _Site({"/": _redirect("ftp://example.com/file")}) as site:
            return await _error_of(site.url())

    assert asyncio.run(scenario()) == "Only http:// and https:// links are supported."


async def _error_of(url: str, policy: FetchPolicy = _OPEN) -> str:
    try:
        await fetch_preview(url, policy)
    except FetchError as exc:
        return _catalogued(exc)
    raise AssertionError("expected a FetchError")


def test_a_redirect_without_a_location_is_reported() -> None:
    async def bare_redirect(request: web.Request) -> web.StreamResponse:
        return web.Response(status=302)

    async def scenario() -> str:
        async with _Site({"/": bare_redirect}) as site:
            return await _error_of(site.url())

    assert asyncio.run(scenario()) == "The site sent a redirect with nowhere to go."


def test_a_malformed_redirect_location_is_reported() -> None:
    async def scenario() -> str:
        async with _Site({"/": _redirect("http://[bad")}) as site:
            return await _error_of(site.url())

    assert asyncio.run(scenario()) == "I couldn't read that link."


@pytest.mark.parametrize("status", [404, 403, 500, 204])
def test_a_non_200_answer_is_reported_with_its_status(status: int) -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(
            status=status, text="" if status == 204 else "x", content_type="text/html"
        )

    async def scenario() -> str:
        async with _Site({"/": handler}) as site:
            return await _error_of(site.url())

    assert asyncio.run(scenario()) == f"The site answered with HTTP {status}."


@pytest.mark.parametrize("kind", ["image/png", "application/json", "text/plain", "application/pdf"])
def test_a_page_that_is_not_html_is_refused(kind: str) -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=b"data", content_type=kind)

    async def scenario() -> str:
        async with _Site({"/": handler}) as site:
            return await _error_of(site.url())

    assert asyncio.run(scenario()) == "That link isn't a web page I can read."


def test_xhtml_is_accepted() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(text="<head><title>X</title>", content_type="application/xhtml+xml")

    async def scenario() -> Preview:
        async with _Site({"/": handler}) as site:
            return await fetch_preview(site.url(), _OPEN)

    assert asyncio.run(scenario()).title == "X"


def test_only_the_first_bytes_of_a_huge_page_are_read() -> None:
    sent: list[int] = []

    async def huge(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/html"})
        await response.prepare(request)
        await response.write(b"<head><title>Early</title>")
        try:
            for _ in range(400):  # up to ~6 MB, far beyond the 64 KB limit below
                await response.write(b"x" * 16384)
                sent.append(1)
                await asyncio.sleep(0)
        except ConnectionResetError, asyncio.CancelledError:
            pass
        return response

    small = FetchPolicy(allowed=lambda ip: True, ports=None, max_bytes=64 * 1024, timeout=3.0)

    async def scenario() -> Preview:
        async with _Site({"/": huge}) as site:
            return await fetch_preview(site.url(), small)

    preview = asyncio.run(scenario())

    assert preview.title == "Early"
    assert len(sent) < 100  # the client stopped reading long before the end


def test_a_title_beyond_the_byte_limit_is_not_seen() -> None:
    body = "<head><!--" + "x" * 5000 + "--><title>Too late</title>"
    small = FetchPolicy(allowed=lambda ip: True, ports=None, max_bytes=1024, timeout=3.0)

    async def scenario() -> Preview:
        async with _Site({"/": _html(body)}) as site:
            return await fetch_preview(site.url(), small)

    assert asyncio.run(scenario()).title is None


def test_a_slow_site_times_out() -> None:
    async def slow(request: web.Request) -> web.StreamResponse:
        await asyncio.sleep(2)
        return web.Response(text="late", content_type="text/html")

    quick = FetchPolicy(allowed=lambda ip: True, ports=None, timeout=0.2)

    async def scenario() -> str:
        async with _Site({"/": slow}) as site:
            return await _error_of(site.url(), quick)

    assert asyncio.run(scenario()) == "The site took too long to answer."


def test_a_refused_connection_is_reported() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # nothing listens here once the socket closes

    assert _fetch_error(f"http://127.0.0.1:{port}/") == "I couldn't connect to that site."


def test_the_production_policy_refuses_a_local_server_without_sending_a_request() -> None:
    no_port_limit = FetchPolicy(ports=None, timeout=3.0)  # default address policy

    async def scenario() -> tuple[str, str, int]:
        async with _Site({"/": _html(_PAGE)}) as site:
            literal = await _error_of(site.url(), no_port_limit)
            assert site.server is not None
            named = await _error_of(f"http://localhost:{site.server.port}/", no_port_limit)
            return literal, named, len(site.requests)

    literal, named, requests = asyncio.run(scenario())

    assert literal == "That address isn't allowed."  # an IP literal, refused up front
    assert named == "That address isn't allowed."  # a name, refused by the resolver
    assert requests == 0


def test_the_production_policy_refuses_a_nonstandard_port() -> None:
    async def scenario() -> str:
        async with _Site({"/": _html(_PAGE)}) as site:
            return await _error_of(site.url(), FetchPolicy(allowed=lambda ip: True))

    assert asyncio.run(scenario()) == "Only the standard web ports (80 and 443) are supported."


def test_a_declared_charset_is_honoured() -> None:
    async def latin1(request: web.Request) -> web.StreamResponse:
        return web.Response(
            body="<head><title>Café</title>".encode("latin-1"),
            headers={"Content-Type": "text/html; charset=iso-8859-1"},
        )

    async def scenario() -> Preview:
        async with _Site({"/": latin1}) as site:
            return await fetch_preview(site.url(), _OPEN)

    assert asyncio.run(scenario()).title == "Café"


def test_an_unknown_charset_falls_back_to_utf8() -> None:
    async def odd(request: web.Request) -> web.StreamResponse:
        return web.Response(
            body="<head><title>Café</title>".encode(),
            headers={"Content-Type": "text/html; charset=x-no-such-charset"},
        )

    async def scenario() -> Preview:
        async with _Site({"/": odd}) as site:
            return await fetch_preview(site.url(), _OPEN)

    assert asyncio.run(scenario()).title == "Café"


def test_invalid_bytes_are_replaced_rather_than_failing() -> None:
    async def broken(request: web.Request) -> web.StreamResponse:
        return web.Response(body=b"<head><title>A\xff\xfeB</title>", content_type="text/html")

    async def scenario() -> Preview:
        async with _Site({"/": broken}) as site:
            return await fetch_preview(site.url(), _OPEN)

    assert asyncio.run(scenario()).title == "A��B"


def test_a_compressed_page_is_decompressed_and_still_limited() -> None:
    page = gzip.compress(b"<head><title>Zipped</title>" + b"x" * 100_000)

    async def zipped(request: web.Request) -> web.StreamResponse:
        return web.Response(
            body=page, headers={"Content-Type": "text/html", "Content-Encoding": "gzip"}
        )

    async def scenario() -> Preview:
        async with _Site({"/": zipped}) as site:
            return await fetch_preview(site.url(), _OPEN)

    assert asyncio.run(scenario()).title == "Zipped"


# ------------------------------------------------------------- limits, options and wiring


def test_a_control_character_in_a_link_is_refused() -> None:
    with pytest.raises(FetchError, match="valid link"):
        validate_url("https://example.com/\x00")


def test_the_resolver_hands_the_hostname_and_port_to_the_lookup() -> None:
    calls: list[tuple[Any, ...]] = []

    async def lookup(host: str, port: int, **kwargs: Any) -> list[Any]:
        calls.append((host, port, kwargs))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    asyncio.run(SafeResolver(lookup=lookup).resolve("example.com", 443, socket.AF_INET))

    assert calls == [("example.com", 443, {"family": socket.AF_INET, "type": socket.SOCK_STREAM})]


def test_the_resolver_defaults_to_port_zero_and_ipv4() -> None:
    calls: list[tuple[Any, ...]] = []

    async def lookup(host: str, port: int, **kwargs: Any) -> list[Any]:
        calls.append((host, port, kwargs["family"]))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    any_port = FetchPolicy(ports=None)
    asyncio.run(SafeResolver(any_port, lookup=lookup).resolve("example.com"))

    assert calls == [("example.com", 0, socket.AF_INET)]


def test_an_ipv6_scope_id_is_split_off_at_the_first_percent_sign() -> None:
    async def lookup(host: str, port: int, **kwargs: Any) -> list[Any]:
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111%eth0", port, 0, 2))]

    (result,) = asyncio.run(SafeResolver(lookup=lookup).resolve("example.com", 80))

    assert result["host"] == "2606:4700::1111"


def test_the_connector_never_caches_dns_or_reuses_connections() -> None:
    from aha.net import _connector

    async def scenario() -> tuple[bool, bool, int | None]:
        connector = _connector(DEFAULT_POLICY)
        try:
            return connector.use_dns_cache, connector.force_close, connector.limit
        finally:
            await connector.close()

    assert asyncio.run(scenario()) == (False, True, 2)


def test_the_session_options_ignore_the_proxy_environment_and_identify_the_bot() -> None:
    from aha.net import _HEADERS, _NO_REDIRECTS, _SESSION_OPTIONS

    assert _SESSION_OPTIONS["trust_env"] is False
    assert _SESSION_OPTIONS["headers"] == {"User-Agent": USER_AGENT, "Accept": "text/html"}
    assert _HEADERS is _SESSION_OPTIONS["headers"]
    assert _NO_REDIRECTS == {"allow_redirects": False}


def test_a_proxy_in_the_environment_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    # If the proxy were honoured the request would go to this unused port and fail.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")

    async def scenario() -> Preview:
        async with _Site({"/": _html(_PAGE)}) as site:
            return await fetch_preview(site.url(), _OPEN)

    assert asyncio.run(scenario()).title == "OG Title"


def test_cookies_are_not_kept_even_for_a_named_host() -> None:
    # aiohttp's default jar ignores cookies for IP-address hosts, so a hostname is needed to show
    # that ours discards them too.
    async def cookie_setter(request: web.Request) -> web.StreamResponse:
        response = web.Response(status=302, headers={"Location": "/page"})
        response.set_cookie("session", "secret")
        return response

    async def scenario() -> list[str | None]:
        async with _Site({"/": cookie_setter, "/page": _html(_PAGE)}) as site:
            assert site.server is not None
            await fetch_preview(f"http://localhost:{site.server.port}/", _OPEN)
            return [request.headers.get("Cookie") for request in site.requests]

    assert asyncio.run(scenario()) == [None, None]


def test_a_redirect_is_followed_by_us_not_by_the_http_client() -> None:
    allow_loopback_only = FetchPolicy(
        allowed=lambda ip: str(ip) == "127.0.0.1", ports=None, timeout=3.0
    )

    async def scenario() -> tuple[str, list[str]]:
        async with _Site({"/": _redirect("http://10.255.255.1/x"), "/x": _html(_PAGE)}) as site:
            message = await _error_of(site.url(), allow_loopback_only)
            return message, [request.path for request in site.requests]

    message, paths = asyncio.run(scenario())

    assert message == "That address isn't allowed."
    assert paths == ["/"]


def _stalled_after(first: bytes) -> Handler:
    """A page that sends ``first`` and then stalls without ever finishing."""

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/html"})
        await response.prepare(request)
        await response.write(first)
        await asyncio.sleep(5)
        return response

    return handler


def test_reading_stops_as_soon_as_the_byte_limit_is_reached() -> None:
    limit = 1024
    policy = FetchPolicy(allowed=lambda ip: True, ports=None, max_bytes=limit, timeout=1.0)
    first = b"<head><title>Done</title><!--" + b"x" * (limit - 29)

    async def scenario() -> Preview:
        async with _Site({"/": _stalled_after(first)}) as site:
            return await fetch_preview(site.url(), policy)

    assert len(first) == limit
    assert asyncio.run(scenario()).title == "Done"  # did not wait for the stalled remainder


def test_reading_waits_while_the_body_is_one_byte_under_the_limit() -> None:
    limit = 1024
    policy = FetchPolicy(allowed=lambda ip: True, ports=None, max_bytes=limit, timeout=0.5)
    first = b"<head><title>Wait</title><!--" + b"x" * (limit - 30)

    async def scenario() -> str:
        async with _Site({"/": _stalled_after(first)}) as site:
            return await _error_of(site.url(), policy)

    assert len(first) == limit - 1
    assert asyncio.run(scenario()) == "The site took too long to answer."


def test_a_body_spanning_several_chunks_is_joined_without_a_separator() -> None:
    body = "<head><title>" + "a" * 40_000 + "</title>"

    async def scenario() -> Preview:
        async with _Site({"/": _html(body)}) as site:
            return await fetch_preview(site.url(), _OPEN)

    assert asyncio.run(scenario()).title == "a" * 40_000


def test_an_interrupted_download_is_reported_and_logged_for_debugging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def cut_short(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            headers={"Content-Type": "text/html", "Content-Length": "5000"}
        )
        await response.prepare(request)
        await response.write(b"<head><tit")
        assert request.transport is not None
        request.transport.close()
        return response

    async def scenario() -> tuple[str, str]:
        async with _Site({"/": cut_short}) as site:
            return await _error_of(site.url(), _OPEN), site.url()

    with caplog.at_level("DEBUG", logger="aha.net"):
        message, url = asyncio.run(scenario())

    assert message == "I couldn't read that link."
    (record,) = [r for r in caplog.records if r.name == "aha.net"]
    assert record.levelname == "DEBUG"
    assert record.getMessage() == f"Fetching {url} failed"
    assert isinstance(record.exc_info, tuple)  # a real exception, not just a flag


def test_pushing_a_cache_entry_again_does_not_evict_an_unrelated_one() -> None:
    cache: TTLCache[int] = TTLCache(ttl=100.0, capacity=2, clock=FakeClock(0.0))
    cache.put("a", 1)
    cache.put("b", 2)

    cache.put("b", 20)  # replaces "b" in place; the cache is not over capacity

    assert (cache.get("a"), cache.get("b")) == (1, 20)


def test_a_title_end_tag_without_a_start_tag_sets_no_title() -> None:
    assert parse_preview("https://x/", "<head>junk</title>").title is None


def test_tags_inside_a_title_are_text_as_in_a_browser() -> None:
    preview = parse_preview("https://x/", "<head><title>A<b>x</b>B</title>")

    assert preview.title == "A<b>x</b>B"


def test_a_meta_attribute_without_a_value_counts_as_empty() -> None:
    preview = parse_preview("https://x/", '<head><meta name="description" content><title>T</title>')

    assert preview.description is None


def test_a_title_delivered_in_several_pieces_is_joined() -> None:
    from aha.net import _HeadParser

    parser = _HeadParser()
    parser.handle_starttag("title", [])
    parser.handle_data("Hel")
    parser.handle_data("lo ")
    parser.handle_data("World")
    parser.handle_endtag("title")

    assert parser.title == "Hello World"


@pytest.mark.parametrize("charset", ["rot13", "base64", "zlib", "hex", "no-such-codec"])
def test_a_charset_that_is_not_a_text_encoding_falls_back_to_utf8(charset: str) -> None:
    # codecs.lookup() accepts the first four, but they cannot decode text and would raise later.
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(
            body="<head><title>Café</title>".encode(),
            headers={"Content-Type": f"text/html; charset={charset}"},
        )

    async def scenario() -> Preview:
        async with _Site({"/": handler}) as site:
            return await fetch_preview(site.url(), _OPEN)

    assert asyncio.run(scenario()).title == "Café"


def test_a_utf16_page_with_an_odd_byte_count_is_decoded_leniently() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(
            body=b"<\x00h\x00e\x00a\x00d\x00>\x00x",  # truncated mid character
            headers={"Content-Type": "text/html; charset=utf-16-le"},
        )

    async def scenario() -> Preview:
        async with _Site({"/": handler}) as site:
            return await fetch_preview(site.url(), _OPEN)

    assert asyncio.run(scenario()).title is None  # no title, but no crash either


def test_a_fetch_error_carries_its_catalogue_key_and_values() -> None:
    error = FetchError("The site answered with HTTP 503.", "net.http_status", status=503)

    assert str(error) == "The site answered with HTTP 503."
    assert (error.key, error.values) == ("net.http_status", {"status": 503})
    assert FetchError("x", "net.timeout").values == {}
