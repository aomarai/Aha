"""Offline tests for the command surface (access control included)."""

from __future__ import annotations

import asyncio
import platform
from typing import Any, cast

import discord
import pytest
from discord.ext import commands

from aha.bot import AhaBot
from aha.cogs.meta import Meta
from aha.cogs.meta import setup as meta_setup
from aha.config import Settings
from tests.helpers import Named, StubContext, as_context, assert_reply


def _bot() -> AhaBot:
    return AhaBot(Settings(token="test-token"))


def _run(cog: Meta, ctx: StubContext, method: str) -> None:
    callback = cast("Any", getattr(cog, method).callback)
    asyncio.run(callback(cog, as_context(ctx)))


def test_ping_reports_latency_in_milliseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot()
    monkeypatch.setattr(type(bot), "latency", property(lambda self: 0.042))
    cog = Meta(bot)
    ctx = StubContext()

    _run(cog, ctx, "ping")

    content, kwargs = ctx.replies[0]
    assert content is None
    assert "embed" in kwargs
    embed = kwargs["embed"]
    assert embed.description == "WebSocket latency: 42 ms"


def test_ping_falls_back_to_plain_text_when_embed_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()
    monkeypatch.setattr(type(bot), "latency", property(lambda self: 0.042))
    cog = Meta(bot)
    ctx = StubContext(reject_embeds=True)

    _run(cog, ctx, "ping")

    assert_reply(ctx.replies, "WebSocket latency: 42 ms")


@pytest.mark.parametrize("latency", [float("inf"), float("nan")])
def test_ping_before_the_first_heartbeat_does_not_crash(
    monkeypatch: pytest.MonkeyPatch, latency: float
) -> None:
    bot = _bot()
    monkeypatch.setattr(type(bot), "latency", property(lambda self: latency))
    ctx = StubContext()

    _run(Meta(bot), ctx, "ping")

    assert ctx.replies[0][1]["embed"].description == "WebSocket latency: n/a"


def _about_bot(monkeypatch: pytest.MonkeyPatch) -> AhaBot:
    bot = _bot()
    bot.started_at = 1_700_000_000.9
    monkeypatch.setattr(type(bot), "latency", property(lambda self: 0.042))
    monkeypatch.setattr(type(bot), "guilds", property(lambda self: [Named(), Named(), Named()]))
    monkeypatch.setenv("AHA_VERSION", "1.2.3")
    monkeypatch.setenv("AHA_COMMIT", "0123456789abcdef")
    return bot


def test_about_reports_version_commit_uptime_and_this_processes_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = StubContext()

    _run(Meta(_about_bot(monkeypatch)), ctx, "about")

    content, kwargs = ctx.replies[0]
    assert content is None
    embed = kwargs["embed"]
    assert embed.title == "About Aha"
    assert embed.description == "Source: https://github.com/aomarai/Aha"
    fields = [(f.name, f.value, f.inline) for f in embed.fields]
    assert fields == [
        ("Version", "v1.2.3", True),
        ("Commit", "0123456", True),
        ("Started", "<t:1700000000:R>", True),
        ("Guilds (this process)", "3", True),
        ("Shards (this process)", "0", True),
        ("Latency", "42 ms", True),
        ("Runtime", f"Python {platform.python_version()}, discord.py {discord.__version__}", True),
    ]


def test_about_renders_an_unknown_commit_and_an_unready_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _about_bot(monkeypatch)
    monkeypatch.delenv("AHA_COMMIT")
    monkeypatch.setattr(type(bot), "latency", property(lambda self: float("inf")))
    ctx = StubContext()

    _run(Meta(bot), ctx, "about")

    values = {f.name: f.value for f in ctx.replies[0][1]["embed"].fields}
    assert values["Commit"] == "unknown"
    assert values["Latency"] == "n/a"


def test_about_falls_back_to_plain_text_when_embed_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = StubContext(reject_embeds=True)

    _run(Meta(_about_bot(monkeypatch)), ctx, "about")

    assert_reply(
        ctx.replies,
        "Version: v1.2.3\n"
        "Commit: 0123456\n"
        "Started: <t:1700000000:R>\n"
        "Guilds (this process): 3\n"
        "Shards (this process): 0\n"
        "Latency: 42 ms\n"
        f"Runtime: Python {platform.python_version()}, discord.py {discord.__version__}",
    )


def test_about_has_a_per_user_cooldown_and_works_in_direct_messages() -> None:
    cooldown = Meta.about.cooldown
    assert cooldown is not None
    assert (cooldown.rate, cooldown.per) == (1, 10)
    assert Meta.about._buckets.type is commands.BucketType.user
    assert Meta.about.description == "Show the bot's version, uptime and status."
    assert not any(check for check in Meta.about.checks)  # no guild-only or permission gate


def test_ping_has_a_per_user_cooldown() -> None:
    cooldown = Meta.ping.cooldown
    assert cooldown is not None
    assert (cooldown.rate, cooldown.per) == (1, 5)
    # discord.py exposes the bucket type only through the mapping.
    assert Meta.ping._buckets.type is commands.BucketType.user


def test_command_metadata_matches_the_public_surface() -> None:
    assert Meta.ping.qualified_name == "ping"
    assert Meta.ping.description == "Check the bot's WebSocket latency."


def test_setup_registers_the_meta_cog() -> None:
    bot = _bot()

    asyncio.run(meta_setup(bot))

    cog = bot.get_cog("Meta")
    assert isinstance(cog, Meta)
    assert cog.bot is bot


def test_privacy_summarises_the_policy_privately_with_links() -> None:
    ctx = StubContext()

    _run(Meta(_bot()), ctx, "privacy")

    ((content, kwargs),) = ctx.replies
    assert content is None
    assert kwargs["ephemeral"] is True
    embed = kwargs["embed"]
    assert embed.title == "Privacy"
    assert "does **not** store your messages" in embed.description
    assert "`/forgetme` deletes it" in embed.description
    assert "<https://github.com/aomarai/Aha/blob/main/docs/privacy.md>" in embed.description
    assert "<https://github.com/aomarai/Aha/blob/main/docs/terms.md>" in embed.description


def test_privacy_falls_back_to_plain_text_when_embeds_are_rejected() -> None:
    ctx = StubContext(reject_embeds=True)

    _run(Meta(_bot()), ctx, "privacy")

    ((content, kwargs),) = ctx.replies
    assert content is not None
    assert content.startswith("Aha does **not** store your messages")
    assert kwargs["ephemeral"] is True


def test_privacy_metadata_and_cooldown() -> None:
    assert Meta.privacy.qualified_name == "privacy"
    assert Meta.privacy.description == "Show what the bot stores and where the policy is."
    assert Meta.privacy.extras["category"] == "Info"
    cooldown = Meta.privacy.cooldown
    assert cooldown is not None
    assert (cooldown.rate, cooldown.per) == (1, 10)


def test_ping_answers_in_the_users_language(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot()
    monkeypatch.setattr(type(bot), "latency", property(lambda self: 0.042))
    ctx = StubContext()
    ctx.interaction = Named(locale=discord.Locale.german, guild_locale=None)

    _run(Meta(bot), ctx, "ping")

    assert ctx.replies[0][1]["embed"].description == "WebSocket-Latenz: 42 ms"
