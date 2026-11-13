"""The :class:`AhaBot` client and its lifecycle hooks."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time

import discord
from discord import app_commands
from discord.ext import commands

from aha.config import Settings
from aha.ratelimit import RateLimiter

__all__ = [
    "COG_MODULES",
    "GATEWAY_DISCONNECT_TIMEOUT",
    "GATEWAY_WATCHDOG_INTERVAL",
    "AhaBot",
    "AhaTree",
]

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

#: Seconds the gateway may stay disconnected before the bot closes itself.
GATEWAY_DISCONNECT_TIMEOUT = 300.0

#: How often the watchdog re-checks the connection state.
GATEWAY_WATCHDOG_INTERVAL = 30.0


class AhaBot(commands.Bot):
    """The Aha client.

    Named ``AhaBot`` to avoid shadowing ``discord.Client`` / ``commands.Bot``.
    """

    #: Bot-wide per-user budget: invocations allowed per window, across commands.
    #: Enforced per process; sharding (#2) needs shared state.
    GLOBAL_RATE_LIMIT: tuple[int, float] = (10, 10.0)

    def __init__(
        self,
        settings: Settings,
        *,
        rate_limit: tuple[int, float] = GLOBAL_RATE_LIMIT,
        gateway_timeout: float = GATEWAY_DISCONNECT_TIMEOUT,
        watchdog_interval: float = GATEWAY_WATCHDOG_INTERVAL,
    ) -> None:
        if (
            not math.isfinite(gateway_timeout)
            or gateway_timeout <= 0
            or not math.isfinite(watchdog_interval)
            or watchdog_interval <= 0
        ):
            raise ValueError("gateway_timeout and watchdog_interval must be finite and positive")
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
        self.gateway_timeout = gateway_timeout
        self.watchdog_interval = watchdog_interval
        self._sync_lock = asyncio.Lock()
        self._disconnected_since: float | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        #: Process exit code; the watchdog sets 1 so a supervisor restarts the bot.
        self.exit_code = 0
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
        if self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(
                self._watch_gateway(), name="gateway-watchdog"
            )

    async def close(self) -> None:
        """Cancel the gateway watchdog, then close the client."""
        watchdog = self._watchdog_task
        self._watchdog_task = None
        if watchdog is not None:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
        await super().close()

    async def _watch_gateway(self) -> None:
        """Close the bot when the gateway stays disconnected for too long."""
        while not await self._check_gateway_timeout():
            await asyncio.sleep(self.watchdog_interval)

    async def _check_gateway_timeout(self) -> bool:
        """Return True and close the bot when the disconnect limit is exceeded."""
        since = self._disconnected_since
        if since is None:
            return False
        elapsed = time.monotonic() - since
        if elapsed < self.gateway_timeout:
            return False
        log.critical(
            "Gateway has been disconnected for %.1f s (limit %.1f s); closing the bot",
            elapsed,
            self.gateway_timeout,
        )
        # A non-zero code lets an "on-failure" supervisor restart the process.
        self.exit_code = 1
        # Clear before closing so ``close`` does not try to cancel this task.
        self._watchdog_task = None
        await self.close()
        return True

    async def on_connect(self) -> None:
        """Log a fresh gateway session.

        The disconnect clock is deliberately not cleared here: a resumed
        reconnect dispatches ``on_connect`` before ``on_resumed``, and
        ``on_resumed`` needs the timestamp to report the outage duration.
        """
        log.info("Connected to the Discord gateway")

    async def on_disconnect(self) -> None:
        """Start the disconnect clock (once) and log the lost connection."""
        if self._disconnected_since is None:
            self._disconnected_since = time.monotonic()
        # ``ws`` is None once the connection is torn down and may not carry a
        # code on a failed connect, hence the default.
        code = getattr(self.ws, "_close_code", None)
        if code is None:
            log.warning("Disconnected from the Discord gateway")
        else:
            # discord.py does not pass the close frame to ``on_disconnect``; the
            # code lives on the websocket. The reason is not exposed at all.
            log.warning("Disconnected from the Discord gateway (code %s)", code)

    async def on_shard_ready(self, shard_id: int) -> None:
        """Log a shard becoming ready (only fires under sharding)."""
        log.info("Shard %s is ready", shard_id)

    async def on_resumed(self) -> None:
        """Log how long the gateway was disconnected, then clear the clock."""
        since = self._disconnected_since
        self._disconnected_since = None
        if since is None:
            log.info("Resumed the Discord gateway session")
        else:
            log.info(
                "Resumed the Discord gateway session after %.1f s",
                time.monotonic() - since,
            )

    async def sync_commands(self) -> None:
        """Sync the application command tree to Discord.

        Syncing overwrites the application's remote command set with the local
        tree. Run only one revision at a time -- if two start together, whichever
        syncs last wins (and an older revision can clobber newer definitions).
        """
        async with self._sync_lock:
            if self.settings.dev_guild_id is not None:
                # Guild-scoped sync is instant, which is ideal while developing
                # (global sync can take up to an hour to propagate).
                guild = discord.Object(id=self.settings.dev_guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                # Purge any remotely-synced global commands so enabling, changing,
                # or removing DEV_GUILD_ID later does not leave stale duplicates
                # behind. Clearing the *local* tree is what makes the purge work,
                # so keep the commands and put them back afterwards: a later sync
                # (the `/sync` command) must still have globals to copy to the dev
                # guild, and a failed or cancelled sync must not leave it empty.
                global_commands = self.tree.get_commands(guild=None)
                self.tree.clear_commands(guild=None)
                try:
                    await self.tree.sync()
                finally:
                    for command in global_commands:
                        self.tree.add_command(command)
                log.info("Synced commands to dev guild %s", self.settings.dev_guild_id)
            else:
                await self.tree.sync()
                log.info("Synced global application commands")

    async def on_ready(self) -> None:
        """
        Called when the bot has connected to Discord and is ready to process commands.
        """
        # A fresh (non-resumed) session means the outage, if any, is over.
        self._disconnected_since = None
        if self.user is not None:
            log.info("Logged in as %s (id=%s)", self.user, self.user.id)
        else:
            log.error("on_ready fired but self.user is unset; bot state is inconsistent")


class AhaTree(app_commands.CommandTree[AhaBot]):
    """Command tree that forwards errors to bot listeners.

    ``CommandTree.on_error`` is a method, not an event, so a cog cannot hook it
    directly. Forwarding keeps the handler in a loadable cog. With no listener
    registered (cog unloaded) it falls back to discord.py's default logging.
    """

    async def interaction_check(self, interaction: discord.Interaction[AhaBot]) -> bool:
        """Apply the bot-wide per-user budget to slash invocations.

        Hybrid commands run global checks twice on the slash path (the app
        command's own check pass and ``Command.prepare``), so the tree-level
        check is the single place a slash invocation may consume a token; the
        bot-level check skips interaction contexts.
        """
        bot = self.client
        retry_after = bot.rate_limiter.consume(interaction.user.id)
        if retry_after is None:
            return True
        raise app_commands.CommandOnCooldown(
            app_commands.Cooldown(bot.rate_limiter.rate, bot.rate_limiter.per), retry_after
        )

    async def on_error(
        self, interaction: discord.Interaction[AhaBot], error: app_commands.AppCommandError
    ) -> None:
        if self.client.extra_events.get(f"on_{APP_COMMAND_ERROR_EVENT}"):
            self.client.dispatch(APP_COMMAND_ERROR_EVENT, interaction, error)
        else:
            await super().on_error(interaction, error)
