"""The :class:`AhaBot` client and its lifecycle hooks."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from aha import logctx
from aha.cmdsync import CommandDiff, diff_commands
from aha.config import Settings
from aha.ratelimit import RateLimiter
from aha.tasks import TaskRegistry

__all__ = [
    "COG_MODULES",
    "GATEWAY_DISCONNECT_TIMEOUT",
    "GATEWAY_WATCHDOG_INTERVAL",
    "OPERATOR_COG",
    "WATCHDOG_TASK",
    "AhaBot",
    "AhaTree",
    "SyncResult",
]

log = logging.getLogger(__name__)

#: Cog modules loaded automatically at startup.
COG_MODULES: tuple[str, ...] = (
    "aha.cogs.meta",
    "aha.cogs.errors",
    "aha.cogs.help",
    "aha.cogs.fun",
)

#: Cog holding the operator-only ``/sync`` command; loaded only when ``/sync`` is meant to exist
#: (dev guild or ``SYNC_ON_STARTUP``), never in a production deployment where sync is a Job.
OPERATOR_COG = "aha.cogs.ops"

#: Event dispatched for failed app commands (listeners are ``on_app_command_error``).
APP_COMMAND_ERROR_EVENT = "app_command_error"

#: Seconds the gateway may stay disconnected before the bot closes itself.
GATEWAY_DISCONNECT_TIMEOUT = 300.0

#: Name of the gateway watchdog task in the registry.
WATCHDOG_TASK = "gateway-watchdog"

#: How often the watchdog re-checks the connection state.
GATEWAY_WATCHDOG_INTERVAL = 30.0


@dataclass(frozen=True, slots=True)
class SyncResult:
    """What :meth:`AhaBot.sync_commands` did (or, in a dry run, would do) for one target."""

    target: str
    diff: CommandDiff
    #: True when a sync write was performed; False when skipped or a dry run.
    synced: bool


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
        sync_on_startup: bool | None = None,
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
        #: ``/sync`` exists only where an operator drives syncing by hand: a dev guild or an
        #: explicit ``SYNC_ON_STARTUP`` self-host. Production syncs through ``aha sync``.
        self.sync_command_enabled = settings.sync_on_startup or settings.dev_guild_id is not None
        #: Whether ``setup_hook`` syncs. ``aha sync`` passes False and syncs itself.
        self.sync_on_startup = (
            self.sync_command_enabled if sync_on_startup is None else sync_on_startup
        )
        self.rate_limiter = RateLimiter(rate_limit[0], rate_limit[1])
        self.gateway_timeout = gateway_timeout
        self.watchdog_interval = watchdog_interval
        self._sync_lock = asyncio.Lock()
        self._disconnected_since: float | None = None
        #: Supervised background tasks; cogs must spawn through this, never ``create_task``.
        self.tasks = TaskRegistry()
        #: Process exit code; the watchdog sets 1 so a supervisor restarts the bot.
        self.exit_code = 0
        self.add_check(self._enforce_rate_limit)

    async def _enforce_rate_limit(self, ctx: commands.Context[commands.Bot]) -> bool:
        """Apply the bot-wide per-user budget to message (mention) invocations."""
        if ctx.interaction is not None:
            # Slash invocations are gated once by ``AhaTree.interaction_check``.
            return True
        logctx.bind_context(ctx)
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

        if self.sync_command_enabled:
            await self.load_extension(OPERATOR_COG)
            log.info("Loaded extension %s", OPERATOR_COG)

        if self.sync_on_startup:
            await self.sync_commands()
        else:
            log.info("Command sync skipped at startup (run 'aha sync')")
        if WATCHDOG_TASK not in self.tasks:
            self.tasks.spawn(self._watch_gateway, name=WATCHDOG_TASK, restart=True, critical=True)

    async def close(self) -> None:
        """Cancel the background tasks, then close the client."""
        try:
            await self.tasks.close()
        finally:
            # Even if this coroutine is cancelled while tasks unwind, the client must close.
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
        # ``TaskRegistry.close`` skips the calling task, so this does not cancel itself.
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

    async def sync_commands(self, *, dry_run: bool = False) -> list[SyncResult]:
        """Sync the application command tree to Discord, skipping targets that are unchanged.

        Each target (the dev guild, and the global set) is compared with what Discord holds
        (``cmdsync.diff_commands``); the bulk-overwrite write happens only when something
        differs, which removes redundant writes and the common clobber between two revisions
        with identical trees. Divergent revisions still race, which is why production syncs
        run as the ``aha sync`` Job and never at pod startup.

        With ``DEV_GUILD_ID`` set the global commands are copied to that guild and any
        remotely-synced global commands are purged, so enabling, changing or removing the
        setting never leaves stale duplicates. With ``dry_run`` nothing is written.

        Raises:
            discord.MissingApplicationID: if called before logging in.
        """
        async with self._sync_lock:
            if self.settings.dev_guild_id is None:
                return [await self._sync_target(None, dry_run=dry_run)]
            # Guild-scoped sync is instant, which is ideal while developing (global sync can
            # take up to an hour to propagate).
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            results = [await self._sync_target(guild, dry_run=dry_run)]
            # Clearing the *local* global tree is what makes the purge work, so keep the
            # commands and put them back afterwards: a later sync must still have globals to
            # copy to the dev guild, and a failed or cancelled sync must not leave it empty.
            global_commands = self.tree.get_commands(guild=None)
            self.tree.clear_commands(guild=None)
            try:
                results.append(await self._sync_target(None, dry_run=dry_run))
            finally:
                for command in global_commands:
                    self.tree.add_command(command)
            return results

    async def _sync_target(self, guild: discord.Object | None, *, dry_run: bool) -> SyncResult:
        """Compare one target with Discord and sync it when it differs."""
        application_id = self.application_id
        if application_id is None:
            raise discord.MissingApplicationID
        label = "global" if guild is None else f"dev guild {guild.id}"
        local = [command.to_dict(self.tree) for command in self.tree.get_commands(guild=guild)]
        remote = (
            await self.http.get_global_commands(application_id)
            if guild is None
            else await self.http.get_guild_commands(application_id, guild.id)
        )
        diff = diff_commands(local, remote)
        if diff.unchanged:
            log.info("Skipped sync: %s commands are unchanged", label)
            return SyncResult(label, diff, synced=False)
        changes = ", ".join(diff.lines())
        if dry_run:
            log.info("Dry run: %s commands would change: %s", label, changes)
            return SyncResult(label, diff, synced=False)
        if guild is None:
            await self.tree.sync()
        else:
            await self.tree.sync(guild=guild)
        log.info("Synced %s commands: %s", label, changes)
        return SyncResult(label, diff, synced=True)

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
        logctx.bind_interaction(interaction)
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
