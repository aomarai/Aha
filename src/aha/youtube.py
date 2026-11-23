"""Which links ``/play`` accepts (#170): an allowlist of YouTube video links, normalised to an id.

Nothing the user types ever reaches Lavalink as-is. The text is parsed here, only a video id that
matches YouTube's id shape is kept, and the node is then asked for the canonical
``https://www.youtube.com/watch?v=<id>`` URL built from that id. So a crafted link cannot make the
node fetch another host, another scheme, a playlist, or a local file. The bot fetches nothing
itself.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

__all__ = ["HOSTS", "LinkError", "video_id", "watch_url"]

#: The only hosts a link may name.
HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
)

#: YouTube video ids: 11 characters of letters, digits, ``_`` and ``-``.
_ID = re.compile(r"[A-Za-z0-9_-]{11}")

#: The path prefixes whose next segment is a video id (besides ``/watch?v=`` and ``youtu.be/``).
_ID_PREFIXES = ("/shorts/", "/embed/")


class LinkError(Exception):
    """The text is not a link ``/play`` can use; ``key`` is the message key explaining why."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def video_id(text: str) -> str:
    """Return the video id in a YouTube link, or raise :class:`LinkError`.

    Accepted: ``youtube.com/watch?v=ID`` (also ``www.``, ``m.`` and ``music.``), ``youtu.be/ID``,
    ``youtube.com/shorts/ID`` and ``/embed/ID``, over http or https. A ``list=`` or a ``t=`` next to
    a video is ignored (only that one video is played). A link that is only a playlist is refused.
    """
    link = text.strip().removeprefix("<").removesuffix(">")  # Discord wraps links in <>
    try:
        parts = urlsplit(link)
        port = parts.port
    except ValueError as exc:
        raise LinkError("voice.bad_link") from exc
    if parts.hostname is None or parts.scheme not in ("http", "https"):
        raise LinkError("voice.bad_link")
    host = parts.hostname.lower()
    if host not in HOSTS:
        raise LinkError("voice.bad_link")
    if parts.username is not None or parts.password is not None or port not in (None, 80, 443):
        raise LinkError("voice.bad_link")
    query = parse_qs(parts.query)
    if host == "youtu.be":
        candidate = parts.path.removeprefix("/").partition("/")[0]
    elif parts.path == "/watch" and "v" in query:
        candidate = query["v"][0]
    elif parts.path.startswith(_ID_PREFIXES):
        candidate = parts.path.split("/")[2]  # "/shorts/ID..." always has a third part
    elif parts.path == "/playlist" and "list" in query:
        raise LinkError("voice.playlist_unsupported")
    else:
        raise LinkError("voice.bad_link")
    if not _ID.fullmatch(candidate):
        raise LinkError("voice.bad_link")
    return candidate


def watch_url(video: str) -> str:
    """The canonical link for a video id (what the node is asked to load, and what is shown)."""
    return f"https://www.youtube.com/watch?v={video}"
