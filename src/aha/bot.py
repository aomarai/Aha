"""The :class:`AhaBot` client and its lifecycle hooks."""

from __future__ import annotations

import logging
from typing import cast

import discord
from discord import app_commands
from discord.ext import commands

from aha.config import Settings
from aha.ratelimit import RateLimiter

__all__ = ["COG_MODULES", "AhaBot", "AhaTree"]

log = logging.getLogger(__name__)

#: Cog modules loaded automatically at startup.
COG_MODULES: tuple[str, ...] = (
    "aha.cogs.meta",
    "aha.cogs.errors",
    "aha.cogs.help",
    "aha.cogs.fun",
)

#: Event dispatched for failed app commands (listeners are ``on_app_command_error``).
APP_COMMAND_ERROR_EVENT = "app_command_error"


class AhaTree(app_commands.CommandTree[commands.Bot]):
    """Command tree that forwards errors to bot listeners.

    ``CommandTree.on_error`` is a method, not an event, so a cog cannot hook it
    directly. Forwarding keeps the handler in a loadable cog. With no listener
    registered (cog unloaded) it falls back to discord.py's default logging.
    """

    async def interaction_check(self, interaction: discord.Interaction[commands.Bot]) -> bool:
        """Apply the bot-wide per-user budget to slash invocations.

        Hybrid commands run global checks twice on the slash path (the app
        command's own check pass and ``Command.prepare``), so the tree-level
        check is the single place a slash invocation may consume a token; the
        bot-level check skips interaction contexts.
        """
        bot = cast("AhaBot", self.client)
        retry_after = bot.rate_limiter.consume(interaction.user.id)
        if retry_after is None:
            return True
        raise app_commands.CommandOnCooldown(
            app_commands.Cooldown(bot.rate_limiter.rate, bot.rate_limiter.per), retry_after
        )

    async def on_error(
        self, interaction: discord.Interaction[commands.Bot], error: app_commands.AppCommandError
    ) -> None:
        if self.client.extra_events.get(f"on_{APP_COMMAND_ERROR_EVENT}"):
            self.client.dispatch(APP_COMMAND_ERROR_EVENT, interaction, error)
        else:
            await super().on_error(interaction, error)


class AhaBot(commands.Bot):
    """The Aha client.

    Named ``AhaBot`` to avoid shadowing ``discord.Client`` / ``commands.Bot``.
    """

    #: Bot-wide per-user budget: invocations allowed per window, across commands.
    GLOBAL_RATE_LIMIT: tuple[int, float] = (10, 10.0)

    def __init__(
        self, settings: Settings, *, rate_limit: tuple[int, float] = GLOBAL_RATE_LIMIT
    ) -> None:
        # Default intents only: no privileged intent (notably ``message_content``)
        # is requested, so commands are slash-only. ``commands.Bot`` still needs a
        # prefix; ``when_mentioned`` is the one value that needs no message
        # content (Discord delivers it for messages that @mention the bot) and
        # that discord.py does not warn about when the intent is off.
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            help_command=None,
            tree_cls=AhaTree,
        )
        self.settings = settings
        self.rate_limiter = RateLimiter(rate_limit[0], rate_limit[1])
        self.add_check(self._enforce_rate_limit)

    async def _enforce_rate_limit(self, ctx: commands.Context[commands.Bot]) -> bool:
        """Apply the bot-wide per-user budget to message (mention) invocations."""
        if ctx.interaction is not None:
            # Slash invocations are gated once by ``AhaTree.interaction_check``.
            return True
        retry_after = self.rate_limiter.consume(ctx.author.id)
        if retry_after is None:
            return True
        raise commands.CommandOnCooldown(
            commands.Cooldown(self.rate_limiter.rate, self.rate_limiter.per),
            retry_after,
            commands.BucketType.user,
        )

    async def setup_hook(self) -> None:
        """Load cogs and sync application commands. Runs once, before login."""
        for module in COG_MODULES:
            await self.load_extension(module)
            log.info("Loaded extension %s", module)

        await self.sync_commands()

    async def sync_commands(self) -> None:
        """Sync the application command tree to Discord.

        Syncing overwrites the application's remote command set with the local
        tree. Run only one revision at a time -- if two start together, whichever
        syncs last wins (and an older revision can clobber newer definitions).
        """
        if self.settings.dev_guild_id is not None:
            # Guild-scoped sync is instant, which is ideal while developing
            # (global sync can take up to an hour to propagate).
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            # Purge any remotely-synced global commands so enabling, changing, or
            # removing DEV_GUILD_ID later does not leave stale duplicates behind.
            # Clearing the *local* tree is what makes the purge work, so keep the
            # commands and put them back afterwards: a later sync (the `/sync`
            # command) must still have globals to copy to the dev guild.
            global_commands = self.tree.get_commands(guild=None)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            for command in global_commands:
                self.tree.add_command(command, guild=None)
            log.info("Synced commands to dev guild %s", self.settings.dev_guild_id)
        else:
            await self.tree.sync()
            log.info("Synced global application commands")

    async def on_ready(self) -> None:
        """
        Called when the bot has connected to Discord and is ready to process commands.
        """
        if self.user is not None:
            log.info("Logged in as %s (id=%s)", self.user, self.user.id)
        else:
            log.error("on_ready fired but self.user is unset; bot state is inconsistent")
