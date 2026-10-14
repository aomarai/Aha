"""Tests for :mod:`aha.commands`."""

from __future__ import annotations

from discord.ext import commands

from aha.commands import hybrid_command


async def _callback(ctx: commands.Context[commands.Bot]) -> None:
    """No-op command body used to exercise the decorator."""


def test_hybrid_command_builds_a_hybrid_command() -> None:
    command = hybrid_command(name="ping", description="Ping.")(_callback)

    assert isinstance(command, commands.HybridCommand)
    assert command.name == "ping"
    assert command.description == "Ping."


def test_hybrid_command_defaults_to_the_function_name() -> None:
    # name is left as discord's MISSING sentinel, so the callback's own name wins.
    command = hybrid_command()(_callback)

    assert isinstance(command, commands.HybridCommand)
    assert command.name == "_callback"


def test_hybrid_command_forwards_extra_attributes() -> None:
    command = hybrid_command(name="ping", with_app_command=False)(_callback)

    assert command.with_app_command is False


class _Probe(commands.Cog):
    """Cog exercising the decorator on a method, as ``aha.cogs`` uses it."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @hybrid_command(name="probe", description="Probe command.")
    async def probe(self, ctx: commands.Context[commands.Bot]) -> None:
        """Probe."""


def test_hybrid_command_works_on_a_cog_method() -> None:
    assert isinstance(_Probe.probe, commands.HybridCommand)
    assert _Probe.probe.name == "probe"
    assert _Probe.probe.description == "Probe command."
