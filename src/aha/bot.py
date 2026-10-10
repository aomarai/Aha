"""The :class:`AhaBot` client and its lifecycle hooks."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from aha.config import Settings

__all__ = ["COG_MODULES", "AhaBot"]

log = logging.getLogger(__name__)

#: Cog modules loaded automatically at startup.
COG_MODULES: tuple[str, ...] = ("aha.cogs.meta",)


class AhaBot(commands.Bot):
    """The Aha client.

    Named ``AhaBot`` to avoid shadowing ``discord.Client`` / ``commands.Bot``.
    """

    def __init__(self, settings: Settings) -> None:
        # ``message_content`` is a privileged intent: enable it in the Discord
        # Developer Portal (Bot -> Privileged Gateway Intents) before running.
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(
            command_prefix=commands.when_mentioned_or(settings.command_prefix),
            intents=intents,
            help_command=None,
        )
        self.settings = settings

    async def setup_hook(self) -> None:
        """Load cogs and sync application commands. Runs once, before login."""
        for module in COG_MODULES:
            await self.load_extension(module)
            log.info("Loaded extension %s", module)

        if self.settings.dev_guild_id is not None:
            # Instant, guild-scoped sync is ideal while developing. Global sync
            # can take up to an hour to propagate.
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to dev guild %s", self.settings.dev_guild_id)
        else:
            await self.tree.sync()
            log.info("Synced global application commands")

    async def on_ready(self) -> None:
        assert self.user is not None  # set once the gateway READY event arrives
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)
