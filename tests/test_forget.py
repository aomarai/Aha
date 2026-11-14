"""Offline tests for /forgetme, its confirm button and the purge helper."""

from __future__ import annotations

import asyncio
import re
from typing import Any, cast

import discord
import pytest

from aha.cogs import forget as forget_module
from aha.cogs.forget import CONFIRM_WINDOW, Forget, ForgetButton
from aha.cogs.forget import setup as forget_setup
from aha.privacy import GUILD_TABLES, USER_TABLES, UserPurge, purge_user
from tests.helpers import Named, StubContext, StubInteraction, as_context, as_interaction

NOW = 1_900_000_000.0


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forget_module, "now", lambda: NOW)


class FakeBot:
    def __init__(self, db: Any = None) -> None:
        self.db = db
        self.cogs: list[Any] = []
        self.items: list[Any] = []

    async def add_cog(self, cog: Any) -> None:
        self.cogs.append(cog)

    def add_dynamic_items(self, *items: Any) -> None:
        self.items.extend(items)


class FakeJobs:
    def __init__(self, count: int = 3) -> None:
        self.count = count
        self.users: list[int] = []

    async def purge_user(self, user_id: int) -> int:
        self.users.append(user_id)
        return self.count


class FakeSettings:
    def __init__(self, count: int = 2) -> None:
        self.count = count
        self.users: list[int] = []

    async def anonymise_user(self, user_id: int) -> int:
        self.users.append(user_id)
        return self.count


def test_purge_user_clears_every_store_and_reports_the_counts() -> None:
    jobs, settings = FakeJobs(3), FakeSettings(2)

    result = asyncio.run(purge_user(jobs, settings, 33))

    assert result == UserPurge(jobs=3, settings=2)
    assert (jobs.users, settings.users) == ([33], [33])


def test_the_purge_covers_the_tables_the_inventory_declares() -> None:
    assert {"scheduled_jobs", "guild_settings"} == USER_TABLES
    assert {"scheduled_jobs", "guild_settings"} == GUILD_TABLES


# ----------------------------------------------------------------------------- command


def run(cog: Forget, ctx: StubContext) -> None:
    asyncio.run(cast("Any", cog.forgetme.callback)(cog, as_context(ctx)))


def test_the_command_asks_for_confirmation_privately_with_an_expiring_button() -> None:
    cog = Forget(cast("Any", FakeBot(db=Named())))
    ctx = StubContext()

    run(cog, ctx)

    ((_, kwargs),) = ctx.replies
    assert kwargs["ephemeral"] is True
    embed = kwargs["embed"]
    assert embed.title == "Delete your data?"
    assert "permanently deletes your pending reminders" in embed.description
    assert "within 5 minutes" in embed.description
    (button,) = kwargs["view"].children
    assert button.custom_id == f"aha:forget:33:{int(NOW) + CONFIRM_WINDOW}"
    assert button.item.label == "Delete my data"
    assert button.item.style == discord.ButtonStyle.danger
    assert kwargs["view"].timeout is None  # persistent: survives restarts


def test_without_a_database_there_is_nothing_to_delete() -> None:
    cog = Forget(cast("Any", FakeBot(db=None)))
    ctx = StubContext()

    run(cog, ctx)

    ((_, kwargs),) = ctx.replies
    assert kwargs["ephemeral"] is True
    assert "nothing to delete" in kwargs["embed"].description
    assert "view" not in kwargs


def test_the_command_metadata() -> None:
    assert Forget.forgetme.qualified_name == "forgetme"
    assert Forget.forgetme.extras["category"] == "Info"
    cooldown = Forget.forgetme.cooldown
    assert cooldown is not None
    assert (cooldown.rate, cooldown.per) == (1, 30)


def test_the_confirm_window_is_five_minutes() -> None:
    assert CONFIRM_WINDOW == 300


# ------------------------------------------------------------------------------ button


def click(button: ForgetButton, interaction: StubInteraction) -> None:
    asyncio.run(button.callback(as_interaction(interaction)))


def interaction_with(bot: FakeBot, *, user_id: int = 33) -> StubInteraction:
    interaction = StubInteraction(user_id=user_id)
    interaction.client = bot
    return interaction


def test_the_template_matches_the_custom_id() -> None:
    match = re.fullmatch(ForgetButton.__discord_ui_compiled_template__.pattern, "aha:forget:33:99")

    assert match is not None
    assert (match["owner"], match["expires"]) == ("33", "99")


def test_a_button_is_rebuilt_from_its_custom_id_alone() -> None:
    match = re.fullmatch(
        ForgetButton.__discord_ui_compiled_template__.pattern, "aha:forget:33:1900000100"
    )
    assert match is not None

    button = asyncio.run(
        ForgetButton.from_custom_id(as_interaction(StubInteraction()), cast("Any", Named()), match)
    )

    assert (button.owner_id, button.expires) == (33, 1_900_000_100)


def test_a_confirmed_click_deletes_the_invokers_data(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs, settings = FakeJobs(4), FakeSettings(1)
    database = Named()
    seen: list[Any] = []

    def make_jobs(db: Any) -> FakeJobs:
        seen.append(db)
        return jobs

    def make_settings(db: Any) -> FakeSettings:
        seen.append(db)
        return settings

    monkeypatch.setattr(forget_module, "JobStore", make_jobs)
    monkeypatch.setattr(forget_module, "GuildSettingsStore", make_settings)
    interaction = interaction_with(FakeBot(db=database))

    click(ForgetButton(33, int(NOW) + 10), interaction)

    assert seen == [database, database]  # both stores sit on the bot's own pool
    assert (jobs.users, settings.users) == ([33], [33])
    (edited,) = interaction.edited
    assert edited["view"] is None  # the button is gone, so it cannot be pressed twice
    assert edited["embed"].description == (
        "Done. Deleted 4 reminder(s) and scheduled job(s) and removed your id from 1 server "
        "setting(s)."
    )


def test_a_click_at_exactly_the_expiry_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs, settings = FakeJobs(), FakeSettings()
    monkeypatch.setattr(forget_module, "JobStore", lambda db: jobs)
    monkeypatch.setattr(forget_module, "GuildSettingsStore", lambda db: settings)
    interaction = interaction_with(FakeBot(db=Named()))

    click(ForgetButton(33, int(NOW)), interaction)

    assert jobs.users == [33]


def test_an_expired_confirmation_deletes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs, settings = FakeJobs(), FakeSettings()
    monkeypatch.setattr(forget_module, "JobStore", lambda db: jobs)
    monkeypatch.setattr(forget_module, "GuildSettingsStore", lambda db: settings)
    interaction = interaction_with(FakeBot(db=Named()))

    click(ForgetButton(33, int(NOW) - 1), interaction)

    assert (jobs.users, settings.users) == ([], [])
    (edited,) = interaction.edited
    assert edited["embed"].description == "This confirmation expired. Run `/forgetme` again."
    assert edited["view"] is None


def test_a_click_without_a_database_says_there_is_nothing_to_delete() -> None:
    interaction = interaction_with(FakeBot(db=None))

    click(ForgetButton(33, int(NOW) + 10), interaction)

    (edited,) = interaction.edited
    assert "nothing to delete" in edited["embed"].description
    assert edited["view"] is None


def test_only_the_invoker_may_confirm() -> None:
    button = ForgetButton(33, int(NOW) + 10)
    stranger = StubInteraction(user_id=99)
    mine = StubInteraction(user_id=33)

    assert asyncio.run(button.interaction_check(as_interaction(stranger))) is False
    assert asyncio.run(button.interaction_check(as_interaction(mine))) is True
    ((kind, content, kwargs),) = stranger.sent
    assert (kind, content, kwargs["ephemeral"]) == (
        "response",
        "This confirmation isn't yours.",
        True,
    )


def test_now_is_unix_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()

    import time

    assert abs(forget_module.now() - time.time()) < 5


def test_setup_registers_the_persistent_button_and_the_cog() -> None:
    bot = FakeBot()

    asyncio.run(forget_setup(cast("Any", bot)))

    assert bot.items == [ForgetButton]
    (cog,) = bot.cogs
    assert cog.bot is bot


# ------------------------------------------------------------------------- German


def test_forgetme_speaks_german_to_a_german_client() -> None:
    cog = Forget(cast("Any", FakeBot(db=Named())))
    ctx = StubContext()
    ctx.interaction = Named(locale=discord.Locale.german, guild_locale=None)

    run(cog, ctx)

    ((_, kwargs),) = ctx.replies
    assert kwargs["embed"].title == "Deine Daten löschen?"
    assert kwargs["embed"].description.startswith("Das löscht deine ausstehenden Erinnerungen")
    assert kwargs["view"].children[0].item.label == "Meine Daten löschen"


def test_the_confirm_click_answers_in_german(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs, settings = FakeJobs(2), FakeSettings(1)
    monkeypatch.setattr(forget_module, "JobStore", lambda db: jobs)
    monkeypatch.setattr(forget_module, "GuildSettingsStore", lambda db: settings)
    interaction = interaction_with(FakeBot(db=Named()))
    interaction.locale = discord.Locale.german

    click(ForgetButton(33, int(NOW) + 10), interaction)
    assert interaction.edited[0]["embed"].description == (
        "Fertig. 2 Erinnerung(en) und geplante Aufgabe(n) gelöscht und deine ID aus 1 "
        "Servereinstellung(en) entfernt."
    )

    expired = interaction_with(FakeBot(db=Named()))
    expired.locale = discord.Locale.german
    click(ForgetButton(33, int(NOW) - 1), expired)
    assert expired.edited[0]["embed"].description == (
        "Diese Bestätigung ist abgelaufen. Führe `/forgetme` erneut aus."
    )

    no_db = interaction_with(FakeBot(db=None))
    no_db.locale = discord.Locale.german
    click(ForgetButton(33, int(NOW) + 10), no_db)
    assert no_db.edited[0]["embed"].description.startswith("Aha speichert in dieser Installation")
