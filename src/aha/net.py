"""Fetching a user-supplied URL without becoming a way into the network the bot runs in.

Anything that fetches a URL a user typed is a server-side request forgery (SSRF) primitive: from
inside a cluster, ``http://10.43.0.1`` is the Kubernetes API and ``http://169.254.169.254`` is a
cloud metadata service. The defences, all here so they cannot be skipped by one caller:

* **Validate what the name resolves to, not what it looks like.** The operating system's resolver
  accepts forms such as ``2130706433`` or ``0x7f.1`` for ``127.0.0.1``, so every hostname goes
  through :class:`SafeResolver`, which refuses a name if *any* of its addresses is not a public,
  globally routable one (:func:`address_allowed`).
* **Connect to the addresses that were checked.** aiohttp connects to what the resolver returned,
  so there is no second lookup an attacker's DNS could answer differently (DNS rebinding).
* **Re-validate every redirect.** Redirects are followed by hand, because aiohttp does not call
  the resolver for an IP literal; each hop passes :func:`validate_url` again.
* **Bound everything:** http(s) on ports 80 and 443 only, no credentials in the URL, a few
  redirects, a total time limit, a byte limit on the body, no cookies and no proxy from the
  environment.

A deployment should add a network-level control as well (an egress NetworkPolicy); this module is
the application layer, not a substitute.
"""

from __future__ import annotations

import asyncio
import contextlib
import html.parser
import ipaddress
import logging
import re
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import yarl
from aiohttp import hdrs
from aiohttp.abc import AbstractResolver, ResolveResult

__all__ = [
    "DEFAULT_POLICY",
    "USER_AGENT",
    "BlockedAddressError",
    "FetchError",
    "FetchPolicy",
    "Preview",
    "SafeResolver",
    "TTLCache",
    "address_allowed",
    "fetch_preview",
    "parse_preview",
    "validate_url",
]

log = logging.getLogger(__name__)

#: Sent with every request so site operators can tell who is calling.
USER_AGENT = "AhaBot (+https://github.com/aomarai/Aha)"

_MAX_URL_LENGTH = 2048
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CHUNK = 16 * 1024
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_FALLBACK_CHARSET = "utf-8"
_DECODE_ERRORS = "replace"

#: Sent with every request, so site operators can tell who is calling.
_HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html"}

#: Session options that matter for safety, kept as plain data so they can be asserted on:
#: ignore proxy variables in the environment, and never keep or replay cookies.
_SESSION_OPTIONS: dict[str, Any] = {"trust_env": False, "headers": _HEADERS}

#: Redirects are followed by hand so every hop is validated (aiohttp does not call the resolver
#: for an IP literal).
_NO_REDIRECTS: dict[str, Any] = {"allow_redirects": False}


class FetchError(Exception):
    """A fetch failed in a way that is safe to explain to the user (the text is shown as is)."""


class BlockedAddressError(OSError):
    """A hostname resolved to an address the policy forbids."""


def address_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True for a public, globally routable unicast address; False for everything else.

    ``ipaddress.is_global`` alone is not enough: it accepts multicast ranges, some reserved ranges
    (NAT64) and the deprecated IPv6 site-local range, so those are denied explicitly. An IPv4
    address wrapped in IPv6 (``::ffff:10.0.0.1``) is judged as the IPv4 address it carries.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.is_site_local:
            return False
    # ``is_global`` already excludes private, loopback, link-local, unspecified and shared
    # (carrier-grade NAT) ranges; it wrongly admits multicast and some reserved ones.
    return ip.is_global and not (ip.is_multicast or ip.is_reserved)


@dataclass(frozen=True, slots=True)
class FetchPolicy:
    """What a fetch may touch. The defaults are the production policy; tests loosen them."""

    allowed: Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address], bool] = address_allowed
    #: Ports a URL may use; ``None`` allows any (tests only).
    ports: frozenset[int] | None = frozenset({80, 443})
    max_bytes: int = 512 * 1024
    timeout: float = 5.0
    max_redirects: int = 3


DEFAULT_POLICY = FetchPolicy()


def validate_url(raw: str, policy: FetchPolicy = DEFAULT_POLICY) -> yarl.URL:
    """Parse ``raw`` and return it as a normalised URL, or raise :class:`FetchError`.

    Checks the parts that can be judged without a network lookup: length, scheme, credentials,
    port, and an IP-literal host. Hostnames are judged by :class:`SafeResolver` when connecting.
    """
    text = raw.strip()
    # ``isprintable`` rejects control characters; whitespace (including a plain space) is checked
    # separately because a space is printable.
    if (
        not text
        or len(text) > _MAX_URL_LENGTH
        or not text.isprintable()
        or any(c.isspace() for c in text)
    ):
        raise FetchError("That doesn't look like a valid link.")
    try:
        url = yarl.URL(text)
        port = url.port
    except ValueError, UnicodeError:
        raise FetchError("That doesn't look like a valid link.") from None
    if url.scheme not in {"http", "https"} or not url.host:
        raise FetchError("Only http:// and https:// links are supported.")
    if url.user is not None or url.password is not None:
        raise FetchError("Links with a username or password are not supported.")
    if policy.ports is not None and port not in policy.ports:
        raise FetchError("Only the standard web ports (80 and 443) are supported.")
    try:
        literal = ipaddress.ip_address(url.host)
    except ValueError:
        return url  # a name: judged when it is resolved
    if not policy.allowed(literal):
        raise FetchError("That address isn't allowed.")
    return url


class SafeResolver(AbstractResolver):
    """An aiohttp resolver that refuses a name unless every address it has is allowed."""

    def __init__(
        self,
        policy: FetchPolicy = DEFAULT_POLICY,
        *,
        lookup: Callable[..., Awaitable[list[Any]]] | None = None,
    ) -> None:
        self._policy = policy
        self._lookup = lookup

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        """Resolve ``host``; raise :class:`BlockedAddressError` if any address is forbidden."""
        if self._policy.ports is not None and port not in self._policy.ports:
            raise BlockedAddressError(f"port {port} is not allowed")
        lookup = self._lookup or asyncio.get_running_loop().getaddrinfo
        infos = await lookup(host, port, family=family, type=socket.SOCK_STREAM)
        results: list[ResolveResult] = []
        for found_family, _type, proto, _canonical, sockaddr in infos:
            # An IPv6 scope id ("fe80::1%eth0") is not part of the address.
            address = ipaddress.ip_address(str(sockaddr[0]).partition("%")[0])
            if not self._policy.allowed(address):
                raise BlockedAddressError(f"{host} resolves to a forbidden address")
            results.append(
                {
                    "hostname": host,
                    "host": str(address),
                    "port": port,
                    "family": found_family,
                    "proto": proto,
                    "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
                }
            )
        if not results:
            raise BlockedAddressError(f"{host} has no addresses")
        return results

    async def close(self) -> None:
        """Nothing to release."""


@dataclass(frozen=True, slots=True)
class Preview:
    """What a page says about itself."""

    url: str
    title: str | None
    description: str | None
    site_name: str | None


class _HeadParser(html.parser.HTMLParser):
    """Collects the title and Open Graph / description meta tags, and stops at the body."""

    class Done(Exception):  # noqa: N818
        """Raised to stop feeding once the head has been read."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.meta: dict[str, str] = {}
        #: Text collected while inside the first ``<title>``; ``None`` outside it.
        self._title_text: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            raise self.Done
        if tag == "title" and self.title is None:
            self._title_text = ""
        elif tag == "meta":
            fields = {name: value or "" for name, value in attrs}
            key = fields.get("property") or fields.get("name")
            if key and "content" in fields:
                self.meta.setdefault(key.lower(), fields["content"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._title_text is not None:
            self.title = _squash(self._title_text)
            self._title_text = None
        elif tag == "head":
            raise self.Done

    def handle_data(self, data: str) -> None:
        if self._title_text is not None:
            self._title_text += data


def _squash(text: str) -> str:
    """Collapse runs of whitespace to single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def parse_preview(url: str, body: str) -> Preview:
    """Extract a title, description and site name from the start of an HTML document."""
    parser = _HeadParser()
    with contextlib.suppress(_HeadParser.Done):
        parser.feed(body)
        parser.close()
    meta = parser.meta

    def clean(value: str | None) -> str | None:
        return _squash(value) or None if value else None

    return Preview(
        url=url,
        title=clean(meta.get("og:title")) or clean(parser.title),
        description=clean(meta.get("og:description")) or clean(meta.get("description")),
        site_name=clean(meta.get("og:site_name")),
    )


async def _read_limited(response: aiohttp.ClientResponse, limit: int) -> bytes:
    """Read at most ``limit`` bytes of the (decompressed) body, then stop."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(_CHUNK):
        chunks.append(chunk)
        total += len(chunk)
        if total >= limit:
            break
    return b"".join(chunks)[:limit]


def _charset(response: aiohttp.ClientResponse) -> str:
    """Return the page's declared charset if it is a real text encoding, else UTF-8.

    The name is attacker-controlled. ``codecs.lookup`` is not enough: it accepts ``rot13``,
    ``base64`` and ``zlib``, which are not text encodings and fail later, when decoding. Encoding
    an empty string rejects both unknown names and those, with a ``LookupError``.
    """
    declared = response.charset
    if declared:
        with contextlib.suppress(LookupError):
            "".encode(declared)
            return declared
    return _FALLBACK_CHARSET


def _decode(response: aiohttp.ClientResponse, body: bytes) -> str:
    return body.decode(_charset(response), errors=_DECODE_ERRORS)


def _connector(policy: FetchPolicy) -> aiohttp.TCPConnector:
    """Build a connector that resolves only through the policy and never reuses a connection."""
    return aiohttp.TCPConnector(
        resolver=SafeResolver(policy), use_dns_cache=False, force_close=True, limit=2
    )


async def fetch_preview(raw_url: str, policy: FetchPolicy = DEFAULT_POLICY) -> Preview:
    """Fetch ``raw_url`` safely and return what the page says about itself.

    Raises:
        FetchError: with a message that is safe to show the user, for any expected failure.
    """
    url = validate_url(raw_url, policy)
    try:
        async with (
            asyncio.timeout(policy.timeout),  # one limit for the whole fetch, redirects included
            aiohttp.ClientSession(
                connector=_connector(policy),
                cookie_jar=aiohttp.DummyCookieJar(),
                **_SESSION_OPTIONS,
            ) as session,
        ):
            for _ in range(policy.max_redirects + 1):
                async with session.get(url, **_NO_REDIRECTS) as response:
                    if response.status in _REDIRECT_STATUSES:
                        location = response.headers.get(hdrs.LOCATION)
                        if not location:
                            raise FetchError("The site sent a redirect with nowhere to go.")
                        url = validate_url(str(url.join(yarl.URL(location))), policy)
                        continue
                    if response.status != 200:
                        raise FetchError(f"The site answered with HTTP {response.status}.")
                    kind = response.content_type.lower()
                    if kind not in _HTML_TYPES:
                        raise FetchError("That link isn't a web page I can read.")
                    body = await _read_limited(response, policy.max_bytes)
                    return parse_preview(str(url), _decode(response, body))
            raise FetchError("The link redirects too many times.")
    except TimeoutError:
        raise FetchError("The site took too long to answer.") from None
    except aiohttp.ClientConnectorError as exc:
        if isinstance(exc.os_error, BlockedAddressError):
            raise FetchError("That address isn't allowed.") from None
        raise FetchError("I couldn't connect to that site.") from None
    except aiohttp.ClientError, UnicodeError, ValueError:
        log.debug("Fetching %s failed", url, exc_info=True)
        raise FetchError("I couldn't read that link.") from None


@dataclass(slots=True)
class TTLCache[V]:
    """A small time-limited cache that forgets its oldest entry when full."""

    ttl: float
    capacity: int
    clock: Callable[[], float] = time.monotonic
    _items: dict[str, tuple[float, V]] = field(default_factory=dict, init=False, repr=False)

    def get(self, key: str) -> V | None:
        """Return the cached value, or ``None`` if missing or older than the time limit."""
        entry = self._items.get(key)
        if entry is None:
            return None
        stored, value = entry
        if self.clock() - stored >= self.ttl:
            del self._items[key]
            return None
        return value

    def put(self, key: str, value: V) -> None:
        """Remember ``value``, evicting the oldest entry first if the cache is full."""
        self._items.pop(key, None)
        if len(self._items) >= self.capacity:
            del self._items[next(iter(self._items))]
        self._items[key] = (self.clock(), value)

    def __len__(self) -> int:
        return len(self._items)
