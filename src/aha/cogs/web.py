"""``/url``: a safe preview of a web page.

Fetching a link a user typed is a server-side request forgery risk, so every request goes through
:func:`aha.net.fetch_preview`, which refuses anything but public web addresses (see that module).
Page text is untrusted: it is neutralised before it is shown, and results are cached briefly so a
link posted by many people is fetched once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord.ext import commands

from aha import embeds
from aha.commands import BotLike, hybrid_command
from aha.net import FetchError, Preview, TTLCache, fetch_preview, validate_url
from aha.replies import reply
from aha.textutil import clip, plain

__all__ = ["CACHE_CAPACITY", "CACHE_TTL", "Web", "setup"]

#: How long a fetched preview is reused, and how many are kept (per process).
CACHE_TTL = 300.0
CACHE_CAPACITY = 256

_TITLE_LIMIT = 200
_DESCRIPTION_LIMIT = 300


class Web(commands.Cog):
    """Commands that read from the web."""

    def __init__(
        self,
        bot: BotLike,
        *,
        fetch: Callable[[str], Awaitable[Preview]] = fetch_preview,
        cache: TTLCache[Preview] | None = None,
    ) -> None:
        self.bot = bot
        self._fetch = fetch
        self._cache = cache if cache is not None else TTLCache(CACHE_TTL, CACHE_CAPACITY)

    @hybrid_command(
        name="url", description="Show a preview of a web page.", extras={"category": "Info"}
    )
    @app_commands.describe(link="The http(s) link to preview.")
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def url(self, ctx: commands.Context[commands.Bot], link: str) -> None:
        """Fetch ``link`` and show its title and description."""
        try:
            normalised = str(validate_url(link))
        except FetchError as exc:
            await reply(ctx, embed=embeds.error(str(exc)), ephemeral=True)
            return
        preview = self._cache.get(normalised)
        if preview is None:
            # A fetch can outlast Discord's 3-second acknowledgement window.
            await ctx.defer()
            try:
                preview = await self._fetch(normalised)
            except FetchError as exc:
                await reply(ctx, embed=embeds.error(str(exc)))
                return
            self._cache.put(normalised, preview)
        await reply(ctx, embed=_embed(preview))


def _embed(preview: Preview) -> discord.Embed:
    """Render a preview, neutralising everything the page said."""
    host = urlsplit(preview.url).hostname or preview.url
    title = clip(plain(preview.title), _TITLE_LIMIT) if preview.title else host
    description = (
        clip(plain(preview.description), _DESCRIPTION_LIMIT)
        if preview.description
        else "This page has no description."
    )
    return embeds.info(
        description,
        title=title,
        fields=[
            ("Site", plain(preview.site_name) if preview.site_name else host, True),
            ("Link", preview.url, False),
        ],
    )


async def setup(bot: BotLike) -> None:
    """Register the cog with the bot (called by ``load_extension``)."""
    await bot.add_cog(Web(bot))
