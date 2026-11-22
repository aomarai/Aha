"""``docs/commands.md`` is generated from the bot's real commands and must stay current."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import discord
import pytest
from discord import app_commands
from scripts import gen_command_docs as gen

from aha.bot import AhaBot


def test_the_committed_command_reference_is_current() -> None:
    assert gen.OUTPUT.read_text(encoding="utf-8") == gen.build_markdown(), (
        "docs/commands.md is out of date; run: uv run python -m scripts.gen_command_docs"
    )


def test_every_slash_command_and_menu_is_documented() -> None:
    bot = asyncio.run(gen._bot())
    text = gen.OUTPUT.read_text(encoding="utf-8")

    names = [
        command.qualified_name
        for command in bot.tree.walk_commands()
        if isinstance(command, app_commands.Command)
    ]
    menus = [
        command.name
        for kind in (discord.AppCommandType.message, discord.AppCommandType.user)
        for command in bot.tree.get_commands(type=kind)
    ]

    assert names and menus
    assert [name for name in names if f"### `/{name}`" not in text] == []
    assert [name for name in menus if f"### {name} (" not in text] == []
    assert "/sync" not in text  # the owner-only operator command is not part of the public surface


def test_the_reference_describes_options_choices_and_limits() -> None:
    text = gen.build_markdown()

    assert "Usage: `/watch <show> [where] [lead]`" in text
    assert "| `where` | no | Where to remind you. Direct messages are private." in text
    assert "Choices: `dm` (Direct message); `channel` (Ping me in this channel)." in text
    assert "*cooldown: 1 per 10 s per user.*" in text
    assert "*cooldown: 1 per 20 s per user; servers only.*" in text  # /summarize
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_a_choice_named_like_its_value_is_listed_once() -> None:
    text = gen.build_markdown()

    assert "Choices: `log-channel`; `disable`; `enable`; `summaries`." in text


def test_categories_follow_the_order_help_uses() -> None:
    text = gen.build_markdown()

    assert text.index("## Info") < text.index("## Fun") < text.index("## Admin")
    assert text.index("## Admin") < text.index("## Context menus")


def test_check_mode_reports_a_stale_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "commands.md"
    monkeypatch.setattr(gen, "OUTPUT", target)
    target.write_text("old\n", encoding="utf-8")

    assert gen.main(["--check"]) == 1
    assert "out of date; run: uv run python -m scripts.gen_command_docs" in capsys.readouterr().out
    assert target.read_text(encoding="utf-8") == "old\n"  # --check never writes


def test_writing_then_checking_agrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "commands.md"
    monkeypatch.setattr(gen, "OUTPUT", target)

    assert gen.main([]) == 0
    assert capsys.readouterr().out == f"Wrote {target}\n"
    assert target.read_text(encoding="utf-8") == gen.build_markdown()
    assert gen.main(["--check"]) == 0


def test_permissions_are_named_when_a_command_needs_them() -> None:
    assert gen._permissions(None) is None
    assert gen._permissions(discord.Permissions.none()) is None
    needs = gen._permissions(discord.Permissions(manage_guild=True, manage_messages=True))
    assert needs == "Manage Guild, Manage Messages"


def test_a_server_wide_cooldown_is_not_described_as_personal() -> None:
    text = gen.build_markdown()

    assert "*cooldown: 1 per 5 s per server; servers only; needs: Manage Guild.*" in text
    assert "cooldown: 1 per 5 s per user." in text


def test_the_bot_wide_limit_is_stated_from_the_bots_own_setting() -> None:
    rate, per = AhaBot.GLOBAL_RATE_LIMIT

    assert (
        f"**{rate} command uses per {per:g}\nseconds across all commands**" in gen.build_markdown()
    )


def test_every_bucket_type_has_a_description() -> None:
    from discord.ext import commands

    assert set(gen._SCOPES) == set(commands.BucketType)


def test_the_file_is_replaced_in_one_step_and_leaves_no_temporary_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "commands.md"
    target.write_text("old\n", encoding="utf-8")

    gen._write_atomically(target, "new\n")

    assert target.read_text(encoding="utf-8") == "new\n"
    assert [path.name for path in tmp_path.iterdir()] == ["commands.md"]


def test_an_interrupted_write_leaves_the_old_file_intact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "commands.md"
    target.write_text("old\n", encoding="utf-8")

    def interrupted(source: str, destination: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", interrupted)

    with pytest.raises(KeyboardInterrupt):
        gen._write_atomically(target, "new\n")

    assert target.read_text(encoding="utf-8") == "old\n"
