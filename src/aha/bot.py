"""The :class:`AhaBot` client and its lifecycle hooks."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from aha import logctx
from aha.cmdsync import CommandDiff, diff_commands
from aha.config import Settings
from aha.db import Database
from aha.dbmigrate import load_migrations, require_current
from aha.gatewaybudget import SessionBudget, await_budget
from aha.guildsettings import PROTECTED_COMMANDS, CommandDisabled, GuildSettingsCache
from aha.guildsettings_store import GuildSettingsStore
from aha.health import HealthServer
from aha.identify import IdentifyCoordinator
from aha.jobstore import JobStore
from aha.metrics import Metrics, ShardState
from aha.ratelimit import LocalBackend, RateLimitBackend, RateLimiter
from aha.scheduler import Scheduler
from aha.tasks import TaskRegistry

__all__ = [
    "COG_MODULES",
    "GATEWAY_DISCONNECT_TIMEOUT",
    "GATEWAY_WATCHDOG_INTERVAL",
    "HEALTH_TASK",
    "OPERATOR_COG",
    "READINESS_DISCONNECT_GRACE",
    "SCHEDULER_TASK",
    "WATCHDOG_TASK",
    "AhaBot",
    "AhaTree",
    "SyncResult",
    "gateway_intents",
]

log = logging.getLogger(__name__)

#: Cog modules loaded automatically at startup.
COG_MODULES: tuple[str, ...] = (
    "aha.cogs.meta",
    "aha.cogs.errors",
    "aha.cogs.help",
    "aha.cogs.fun",
    "aha.cogs.polls",
    "aha.cogs.web",
    "aha.cogs.remind",
    "aha.cogs.config",
    "aha.cogs.observability",
)

#: Cog holding the operator-only ``/sync`` command; loaded only when ``/sync`` is meant to exist
#: (dev guild or ``SYNC_ON_STARTUP``), never in a production deployment where sync is a Job.
OPERATOR_COG = "aha.cogs.ops"

#: Event dispatched for failed app commands (listeners are ``on_app_command_error``).
APP_COMMAND_ERROR_EVENT = "app_command_error"

#: Seconds the gateway may stay disconnected before the bot closes itself.
GATEWAY_DISCONNECT_TIMEOUT = 300.0

#: Name of the durable-job scheduler task in the registry.
SCHEDULER_TASK = "scheduler"

#: Name of the health/metrics server task in the registry.
HEALTH_TASK = "health-server"

#: Seconds a gateway disconnect may last before the pod stops reporting ready.
READINESS_DISCONNECT_GRACE = 30.0

#: Name of the gateway watchdog task in the registry.
WATCHDOG_TASK = "gateway-watchdog"

#: How often the watchdog re-checks the connection state.
GATEWAY_WATCHDOG_INTERVAL = 30.0


def gateway_intents() -> discord.Intents:
    """Return the gateway intents the bot uses, and nothing else.

    ``discord.Intents.default()`` subscribes to every non-privileged event (typing, voice states,
    invites, emoji, reactions, scheduled events, ...), and each unused one costs bandwidth, CPU and
    memory on every shard, which is what limits how many shards one pod can hold (ADR 0001).

    * ``guilds``: the guild, channel and role cache, and ``on_guild_remove``. Required.
    * ``guild_messages`` and ``dm_messages``: only so an ``@Aha`` mention reaches the command
      parser. If that surface is ever dropped these go too (#101), leaving one event stream:
      guild lifecycle.

    Add an intent here only together with the feature that needs it (``voice_states`` for #68).
    """
    return discord.Intents(guilds=True, guild_messages=True, dm_messages=True)


_ACTIVITY_TYPES = {
    "playing": discord.ActivityType.playing,
    "listening": discord.ActivityType.listening,
    "watching": discord.ActivityType.watching,
    "competing": discord.ActivityType.competing,
}


def _activity(settings: Settings) -> discord.Activity | None:
    """Build the presence activity from the settings, or ``None`` when it is switched off."""
    if settings.activity is None:
        return None
    return discord.Activity(type=_ACTIVITY_TYPES[settings.activity_type], name=settings.activity)


@dataclass(frozen=True, slots=True)
class SyncResult:
    """What :meth:`AhaBot.sync_commands` did (or, in a dry run, would do) for one target."""

    target: str
    diff: CommandDiff
    #: True when a sync write was performed; False when skipped or a dry run.
    synced: bool


def _shard_options(settings: Settings) -> dict[str, Any]:
    """The ``shard_count`` / ``shard_ids`` keywords, only for what is configured.

    discord.py rejects ``shard_ids`` without ``shard_count`` and requires a list (not a tuple or
    a generator), so nothing is passed for an unset value and ids are always copied to a list.
    """
    options: dict[str, Any] = {}
    if settings.shard_count is not None:
        options["shard_count"] = settings.shard_count
    if settings.shard_ids is not None:
        options["shard_ids"] = list(settings.shard_ids)
    return options


class AhaBot(commands.AutoShardedBot):
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
        health_server: bool = True,
        use_database: bool = True,
        gateway: bool = True,
    ) -> None:
        if (
            not math.isfinite(gateway_timeout)
            or gateway_timeout <= 0
            or not math.isfinite(watchdog_interval)
            or watchdog_interval <= 0
        ):
            raise ValueError("gateway_timeout and watchdog_interval must be finite and positive")
        # No privileged intent (notably ``message_content``) is requested. ``commands.Bot``
        # still needs a prefix; ``when_mentioned`` is the one value that needs no message
        # content (Discord delivers it for messages that @mention the bot) and that discord.py
        # does not warn about when the intent is off.
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=gateway_intents(),
            # Slash commands and mentions never read old messages, so keep none: this is a
            # per-shard cache that would otherwise hold the last 1000 messages of every channel.
            max_messages=None,
            help_command=None,
            tree_cls=AhaTree,
            # Presence is sent when each shard identifies, so it needs no extra task and is
            # applied to every shard this process owns. It needs no privileged intent.
            activity=_activity(settings),
            status=discord.Status(settings.status),
            # Explicit shard assignment (ADR 0001 section 2). ``shard_ids`` must be a list and
            # needs ``shard_count``; ``Settings`` guarantees both. Unset lets Discord decide.
            **_shard_options(settings),
        )
        self.settings = settings
        #: The PostgreSQL pool, opened in ``setup_hook`` when ``DATABASE_URL`` is set.
        self.db: Database | None = None
        #: Runs durable jobs from the database; ``None`` without one. Cogs register handlers on it.
        self.scheduler: Scheduler | None = None
        self.guild_settings: GuildSettingsCache | None = None
        #: False for REST-only jobs (``aha sync``): no gateway session, so nothing to guard.
        self.gateway_enabled = gateway
        #: Serialises IDENTIFY across pods (needs a database and an explicit shard assignment).
        self.identify: IdentifyCoordinator | None = None
        #: How the session-budget guard waits for Discord's reset (a seam for tests).
        self.budget_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        #: Identifies allowed per 5 s per bucket; the real value is read from Discord at startup.
        self.max_concurrency = 1
        #: False for short-lived jobs (``aha sync``) that must not open the database.
        self.use_database = use_database
        #: Unix time the bot object was created, shown by ``/about`` as the start of its uptime.
        self.started_at = time.time()
        #: Whether ``setup_hook`` starts the health server. ``aha sync`` passes False: a short
        #: REST-only Job must not listen on a port.
        self.health_server_enabled = health_server and settings.health_port != 0
        #: ``/sync`` exists only where an operator drives syncing by hand: a dev guild or an
        #: explicit ``SYNC_ON_STARTUP`` self-host. Production syncs through ``aha sync``.
        self.sync_command_enabled = settings.sync_on_startup or settings.dev_guild_id is not None
        #: Whether ``setup_hook`` syncs. ``aha sync`` passes False and syncs itself.
        self.sync_on_startup = (
            self.sync_command_enabled if sync_on_startup is None else sync_on_startup
        )
        #: Per-process today; a shared backend can replace it without touching the checks.
        self.rate_limiter: RateLimitBackend = LocalBackend(
            RateLimiter(rate_limit[0], rate_limit[1])
        )
        self.gateway_timeout = gateway_timeout
        self.watchdog_interval = watchdog_interval
        self._sync_lock = asyncio.Lock()
        #: Monotonic time each owned shard went down, for shards that are down right now.
        self._shard_down: dict[int, float] = {}
        #: Supervised background tasks; cogs must spawn through this, never ``create_task``.
        self.tasks = TaskRegistry()
        self.metrics = Metrics(self.shard_states, self.guild_counts)
        #: Process exit code; the watchdog sets 1 so a supervisor restarts the bot.
        self.exit_code = 0
        self.add_check(self._enforce_rate_limit)

    async def _enforce_rate_limit(self, ctx: commands.Context[commands.Bot]) -> bool:
        """Apply the bot-wide per-user budget to message (mention) invocations."""
        if ctx.interaction is not None:
            # Slash invocations are gated once by ``AhaTree.interaction_check``.
            return True
        logctx.bind_context(ctx)
        self.metrics.start_timer()
        retry_after = await self.rate_limiter.consume(ctx.author.id)
        if retry_after is None:
            await self.ensure_command_enabled(
                ctx.guild.id if ctx.guild else None,
                ctx.command.qualified_name if ctx.command else None,
            )
            return True
        raise commands.CommandOnCooldown(
            commands.Cooldown(self.rate_limiter.rate, self.rate_limiter.per),
            retry_after,
            commands.BucketType.user,
        )

    async def ensure_command_enabled(self, guild_id: int | None, command: str | None) -> None:
        """Raise :class:`CommandDisabled` when an admin turned ``command`` off in this guild.

        Fails open: a settings outage must not take every command down with it.
        """
        if guild_id is None or command is None or self.guild_settings is None:
            return
        root = command.partition(" ")[0]
        if root in PROTECTED_COMMANDS:
            return
        try:
            settings = await self.guild_settings.get(guild_id)
        except Exception:
            log.warning("Could not read guild settings; allowing the command", exc_info=True)
            return
        if command in settings.disabled_commands or root in settings.disabled_commands:
            raise CommandDisabled(f"{command} is disabled in this server")

    async def setup_hook(self) -> None:
        """Load cogs and sync application commands. Runs once, before login."""
        if self.shard_ids is None:
            log.info("Owning every shard (the count is chosen by Discord)")
        else:
            log.info("Owning shards %s of %s", sorted(self.shard_ids), self.shard_count)
        if self.gateway_enabled:
            await self._guard_session_budget()
        if self.use_database and self.settings.database_url is not None:
            await self._connect_database(self.settings.database_url)

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
        if self.scheduler is not None and SCHEDULER_TASK not in self.tasks:
            self.tasks.spawn(self.scheduler.run, name=SCHEDULER_TASK, restart=True, critical=True)
        if self.health_server_enabled and HEALTH_TASK not in self.tasks:
            server = HealthServer(self, self.settings.health_bind, self.settings.health_port)
            self.tasks.spawn(server.run, name=HEALTH_TASK, restart=True, critical=True)
        if WATCHDOG_TASK not in self.tasks:
            self.tasks.spawn(self._watch_gateway, name=WATCHDOG_TASK, restart=True, critical=True)

    async def _guard_session_budget(self) -> None:
        """Log and export the session-start budget; wait for the reset instead of spending the rest.

        Not being able to read the budget never blocks startup: Discord enforces the limit anyway.
        """

        async def fetch() -> SessionBudget:
            _, _, limit = await self.http.get_bot_gateway()
            return SessionBudget.from_payload(limit)

        try:
            budget = await await_budget(
                fetch,
                floor=self.settings.session_start_floor,
                on_budget=self.metrics.set_session_budget,
                sleep=self.budget_sleep,
            )
        except discord.DiscordException:
            log.warning("Could not read the gateway session budget; continuing", exc_info=True)
            return
        self.max_concurrency = budget.max_concurrency

    async def before_identify_hook(self, shard_id: int | None, *, initial: bool = False) -> None:
        """Take the cross-pod identify lock for the shard's bucket when coordination is on."""
        if self.identify is not None and shard_id is not None:
            await self.identify.acquire(shard_id % self.max_concurrency)
            return
        await super().before_identify_hook(shard_id, initial=initial)

    async def _connect_database(self, url: str) -> None:
        """Open the pool and refuse to run against a schema that is behind this build.

        A schema *ahead* of this build is fine: that is what a rollback looks like, and migrations
        are expand/contract so the previous release keeps working.
        """
        self.db = await Database.connect(
            url,
            max_size=self.settings.db_pool_max,
            statement_timeout=self.settings.db_statement_timeout,
        )
        async with self.db.acquire() as connection:
            await require_current(connection, load_migrations())
        self.scheduler = Scheduler(JobStore(self.db), on_lag=self.metrics.scheduler_lag.set)
        self.guild_settings = GuildSettingsCache(GuildSettingsStore(self.db))
        if self.gateway_enabled and self.settings.shard_ids is not None:
            self.identify = IdentifyCoordinator(
                self.db, lambda factory, name: self.tasks.spawn(factory, name=name)
            )
        log.info("Connected to the database; the schema is up to date")

    async def close(self) -> None:
        """Cancel the background tasks, close the database pool, then close the client."""
        try:
            await self.tasks.close()
        finally:
            # Even if this coroutine is cancelled while tasks unwind, the client must close.
            try:
                if self.db is not None:
                    await self.db.close()
            finally:
                await super().close()

    async def _watch_gateway(self) -> None:
        """Close the bot when the gateway stays disconnected for too long."""
        while not await self._check_gateway_timeout():
            await asyncio.sleep(self.watchdog_interval)

    async def _check_gateway_timeout(self) -> bool:
        """Return True and close the bot when the disconnect limit is exceeded."""
        if not self._shard_down:
            return False
        elapsed = time.monotonic() - min(self._shard_down.values())
        if elapsed < self.gateway_timeout:
            return False
        log.critical(
            "Gateway has been disconnected for %.1f s (limit %.1f s); closing the bot",
            elapsed,
            self.gateway_timeout,
            extra={"shards": sorted(self._shard_down)},
        )
        # A non-zero code lets an "on-failure" supervisor restart the process.
        self.exit_code = 1
        # ``TaskRegistry.close`` skips the calling task, so this does not cancel itself.
        await self.close()
        return True

    async def on_connect(self) -> None:
        """Log a fresh gateway session.

        The shard clocks are deliberately not cleared here: a resumed reconnect dispatches
        ``on_connect`` before ``on_shard_resumed``, which needs the timestamp to report the
        outage duration.
        """
        log.info("Connected to the Discord gateway")

    def shard_states(self) -> list[ShardState]:
        """Snapshot every shard this process owns, for readiness and metrics.

        A disconnect shorter than :data:`READINESS_DISCONNECT_GRACE` does not make a shard
        unready: discord.py resumes in-flight sessions within seconds and a probe should not flap
        on that. Each shard has its own clock, so one shard resuming never hides another that
        is still down.
        """
        now = time.monotonic()
        latencies = dict(self.latencies)
        ready = self.is_ready()
        states = []
        for shard_id in self._owned_shard_ids():
            since = self._shard_down.get(shard_id)
            down_too_long = since is not None and now - since > READINESS_DISCONNECT_GRACE
            states.append(
                ShardState(shard_id, ready and not down_too_long, latencies.get(shard_id, math.nan))
            )
        return states

    def _owned_shard_ids(self) -> list[int]:
        """The shard ids this process owns: connected ones, else the configured, else ``[0]``."""
        return sorted(self.shards) or sorted(self.shard_ids or [0])

    def guild_counts(self) -> dict[int, int]:
        """Return the number of cached guilds per owned shard id (zero when it has none)."""
        counts = dict.fromkeys((state.shard_id for state in self.shard_states()), 0)
        counts.update(Counter(guild.shard_id for guild in self.guilds))
        return counts

    async def on_shard_disconnect(self, shard_id: int) -> None:
        """Start the shard's disconnect clock (once) and log the lost connection."""
        self.metrics.reconnects.labels(shard=str(shard_id)).inc()
        self._shard_down.setdefault(shard_id, time.monotonic())
        # discord.py does not pass the close frame to the event; the code lives on the shard's
        # websocket, which is gone once the connection is torn down, hence the defaults.
        parent = getattr(self.shards.get(shard_id), "_parent", None)
        code = getattr(getattr(parent, "ws", None), "_close_code", None)
        if code is None:
            log.warning("Shard %s disconnected from the Discord gateway", shard_id)
        else:
            log.warning("Shard %s disconnected from the Discord gateway (code %s)", shard_id, code)

    async def on_shard_ready(self, shard_id: int) -> None:
        """Log a shard becoming ready; a fresh session means its outage, if any, is over."""
        self._shard_down.pop(shard_id, None)
        log.info("Shard %s is ready", shard_id)

    async def on_shard_resumed(self, shard_id: int) -> None:
        """Log how long a shard was disconnected, then clear its clock."""
        since = self._shard_down.pop(shard_id, None)
        if since is None:
            log.info("Shard %s resumed the Discord gateway session", shard_id)
        else:
            log.info(
                "Shard %s resumed the Discord gateway session after %.1f s",
                shard_id,
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
        bot.metrics.start_timer()
        retry_after = await bot.rate_limiter.consume(interaction.user.id)
        if retry_after is None:
            await bot.ensure_command_enabled(
                interaction.guild_id,
                interaction.command.qualified_name if interaction.command else None,
            )
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
