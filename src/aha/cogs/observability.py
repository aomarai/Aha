"""Counts command outcomes and durations, in one place.

Every command invocation ends in exactly one of these events, so counting here (rather than in
each cog) keeps the numbers consistent and new commands covered automatically:

* ``on_command_completion``: a command that finished, for message *and* hybrid slash commands.
  discord.py also fires ``app_command_completion`` for a hybrid slash command, which would
  double-count, so that event is used only for **context menus**, which have no other event.
  Every other command goes through :func:`aha.commands.hybrid_command`, so none is missed.
* ``on_command_error``: failures of message and hybrid commands.
* ``on_app_command_error``: failures raised by the command tree itself, such as the bot-wide
  rate limit rejecting a slash invocation before the command runs.

Outcomes are ``ok``, ``error``, ``denied`` (a check or permission failed), and ``cooldown``.

Completed commands are also written to the ``aha.audit`` logger (see :data:`AUDIT_LOGGER`) as an
audit trail of who ran what, where, and how long it took. Failures are already logged, with their
context, by :mod:`aha.cogs.errors`.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from aha.bot import AhaBot
from aha.cogs.errors import WRAPPERS
from aha.metrics import Metrics

__all__ = ["AUDIT_LOGGER", "Observability", "outcome_of", "setup"]

#: Logger for the audit trail of completed commands; filter on it to route or silence the trail.
AUDIT_LOGGER = "aha.audit"

audit = logging.getLogger(AUDIT_LOGGER)

#: discord.py logs every 429 it handles on this logger (#103); it is the only place they show up.
HTTP_LOGGER = "discord.http"

#: Start of the message discord.py logs for a rate limit on the whole bot token.
_GLOBAL_429 = "Global rate limit has been hit"
#: Start of the message it logs for a per-route (or shared-bucket) 429.
_ROUTE_429 = "We are being rate limited."


class RateLimitCounter(logging.Handler):
    """Counts the 429s discord.py reports, as ``aha_rest_rate_limited_total{kind}``.

    discord.py handles a 429 internally (it sleeps and retries), so the log line is the only
    signal. Discord's ``X-RateLimit-Scope`` header is not exposed, so the kinds are the two
    messages discord.py distinguishes: ``global`` (the whole token, the dangerous one) and
    ``route`` (a bucket). It never alters or drops a record.
    """

    def __init__(self, metrics: Metrics) -> None:
        super().__init__(level=logging.WARNING)
        self._metrics = metrics

    def emit(self, record: logging.LogRecord) -> None:
        message = str(record.msg)
        if message.startswith(_GLOBAL_429):
            self._metrics.rest_rate_limited.labels(kind="global").inc()
        elif message.startswith(_ROUTE_429):
            self._metrics.rest_rate_limited.labels(kind="route").inc()


def outcome_of(error: Exception) -> str:
    """Classify a command failure for the ``outcome`` label."""
    while isinstance(error, WRAPPERS):
        error = error.original
    if isinstance(error, (commands.CommandOnCooldown, app_commands.CommandOnCooldown)):
        return "cooldown"
    if isinstance(error, (commands.CheckFailure, app_commands.CheckFailure)):
        return "denied"
    return "error"


class Observability(commands.Cog):
    """Feeds command results into :class:`aha.metrics.Metrics`."""

    def __init__(self, bot: AhaBot) -> None:
        self.bot = bot
        self._rate_limits = RateLimitCounter(bot.metrics)

    async def cog_load(self) -> None:
        """Start counting 429s. The logger must let warnings through even at a quieter LOG_LEVEL."""
        logger = logging.getLogger(HTTP_LOGGER)
        logger.addHandler(self._rate_limits)
        if not logger.isEnabledFor(logging.WARNING):
            logger.setLevel(logging.WARNING)

    async def cog_unload(self) -> None:
        """Stop counting."""
        logging.getLogger(HTTP_LOGGER).removeHandler(self._rate_limits)

    @commands.Cog.listener("on_command_completion")
    async def on_command_completion(self, ctx: commands.Context[commands.Bot]) -> None:
        """Count a command that ran to completion."""
        if ctx.command is not None:
            self._completed(ctx.command.qualified_name, sorted(ctx.kwargs))

    @commands.Cog.listener("on_app_command_completion")
    async def on_app_command_completion(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command[Any, ..., Any] | app_commands.ContextMenu,
    ) -> None:
        """Count a context menu that ran to completion (hybrid commands are counted above)."""
        if isinstance(command, app_commands.ContextMenu):
            self._completed(command.qualified_name, [])

    def _completed(self, name: str, arguments: list[str]) -> None:
        """Count a finished command and write its audit record."""
        duration = self.bot.metrics.record_command(name, "ok")
        # Who, where and which command come from the invocation's log context (see
        # aha.logctx), so JSON logs carry shard/guild/channel/user ids without repeating them
        # here. Only the *names* of the arguments are recorded, never their values: those are
        # user content (reminder text, URLs) and fall under the retention policy (#99).
        audit.info(
            "Command /%s completed",
            name,
            extra={
                "event": "command",
                "outcome": "ok",
                "arguments": arguments,
                "duration_ms": None if duration is None else round(duration * 1000),
            },
        )

    @commands.Cog.listener("on_command_error")
    async def on_command_error(
        self, ctx: commands.Context[commands.Bot], error: commands.CommandError
    ) -> None:
        """Count a failed message or hybrid command (unknown commands are not commands)."""
        if ctx.command is not None:
            self.bot.metrics.record_command(ctx.command.qualified_name, outcome_of(error))

    @commands.Cog.listener("on_app_command_error")
    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Count a failure raised by the command tree (rate limit, tree-level checks)."""
        if interaction.command is not None:
            self.bot.metrics.record_command(interaction.command.qualified_name, outcome_of(error))


async def setup(bot: AhaBot) -> None:
    """Register the cog with the bot (called by ``load_extension``)."""
    await bot.add_cog(Observability(bot))
