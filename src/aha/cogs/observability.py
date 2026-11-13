"""Counts command outcomes and durations, in one place.

Every command invocation ends in exactly one of these events, so counting here (rather than in
each cog) keeps the numbers consistent and new commands covered automatically:

* ``on_command_completion``: a command that finished, for message *and* hybrid slash commands.
  discord.py also fires ``app_command_completion`` for a hybrid slash command, which would
  double-count, so that event is deliberately not used. Every command goes through
  :func:`aha.commands.hybrid_command`, so none is missed.
* ``on_command_error``: failures of message and hybrid commands.
* ``on_app_command_error``: failures raised by the command tree itself, such as the bot-wide
  rate limit rejecting a slash invocation before the command runs.

Outcomes are ``ok``, ``error``, ``denied`` (a check or permission failed), and ``cooldown``.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from aha.bot import AhaBot
from aha.cogs.errors import WRAPPERS

__all__ = ["Observability", "outcome_of", "setup"]


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

    @commands.Cog.listener("on_command_completion")
    async def on_command_completion(self, ctx: commands.Context[commands.Bot]) -> None:
        """Count a command that ran to completion."""
        if ctx.command is not None:
            self.bot.metrics.record_command(ctx.command.qualified_name, "ok")

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
