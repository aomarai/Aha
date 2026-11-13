"""Shared stubs and cast helpers for the offline test suite.

Discord objects are replaced with minimal fakes that record what the code under
test does to them, so tests can assert on exact replies and arguments. The
``as_*`` helpers narrow a stub to the real type the code expects, keeping mypy
strict happy without ``# type: ignore``.
"""

from __future__ import annotations

from typing import Any, cast

import discord
from discord.ext import commands

from aha.bot import AhaBot

__all__ = [
    "FakeClock",
    "Named",
    "StubBot",
    "StubContext",
    "StubInteraction",
    "as_bot",
    "as_context",
    "as_interaction",
    "as_tree_interaction",
    "assert_no_mentions",
    "assert_reply",
    "http_error",
    "stub_command",
]


def assert_no_mentions(allowed: discord.AllowedMentions) -> None:
    """Assert every mention type is disabled."""
    assert allowed.everyone is False
    assert allowed.users is False
    assert allowed.roles is False
    assert allowed.replied_user is False


def assert_reply(
    replies: list[tuple[str | None, dict[str, Any]]],
    content: str | None,
    **kwargs: Any,
) -> None:
    """Assert a single recorded reply has ``content``, ``kwargs``, and no mentions."""
    ((actual_content, actual_kwargs),) = replies
    assert actual_content == content
    assert {key: value for key, value in actual_kwargs.items() if key != "allowed_mentions"} == (
        kwargs
    )
    assert_no_mentions(actual_kwargs["allowed_mentions"])


class FakeClock:
    """Deterministic monotonic clock for limiter tests."""

    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Named:
    """Bag of attributes: ``Named(id=1)`` behaves like a tiny Discord object."""

    def __init__(self, **attrs: Any) -> None:
        self.__dict__.update(attrs)


class StubBot:
    """Stand-in exposing only what the cogs under test use."""

    def __init__(self, cmds: list[Any] | None = None, latency: float = 0.0) -> None:
        self.cmds = cmds or []
        self.latency = latency
        self.added_cogs: list[commands.Cog] = []

    def walk_commands(self) -> list[Any]:
        return self.cmds

    async def add_cog(self, cog: commands.Cog) -> None:
        self.added_cogs.append(cog)


class StubContext:
    """Records replies instead of talking to Discord.

    Ids: command ``ping``, guild 11, channel 22, author 33. Set ``fail_with`` to
    make every ``reply`` raise; set ``reject_embeds`` to make only embed-only
    replies raise, as in a channel without embed permission.
    """

    def __init__(
        self, *, command: bool = True, guild: bool = True, reject_embeds: bool = False
    ) -> None:
        self.command = Named(qualified_name="ping") if command else None
        self.guild = Named(id=11) if guild else None
        self.channel = Named(id=22)
        self.author = Named(id=33)
        #: Cached permissions as read by ``commands.has_permissions``.
        self.permissions = discord.Permissions.none()
        #: ``None`` for message contexts; set it to model a hybrid slash invocation.
        self.interaction: Any = None
        #: ``ephemeral`` flags passed to :meth:`defer`.
        self.deferred: list[bool] = []
        self.replies: list[tuple[str | None, dict[str, Any]]] = []
        self.fail_with: Exception | None = None
        self.reject_embeds = reject_embeds

    async def reply(self, content: str | None = None, **kwargs: Any) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        if self.reject_embeds and content is None:
            raise http_error()
        self.replies.append((content, kwargs))

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.deferred.append(ephemeral)


class StubInteraction:
    """Records responses, followups and edits.

    ``sent`` holds ``(kind, content, kwargs)`` for ``response.send_message``
    (``"response"``) and ``followup.send`` (``"followup"``); ``edited`` holds the
    kwargs of each ``response.edit_message``. Ids: command ``ping``, guild 11,
    channel 22, user ``user_id`` (default 33).
    """

    def __init__(self, *, done: bool = False, command: bool = True, user_id: int = 33) -> None:
        self.command = Named(qualified_name="ping") if command else None
        self.guild_id = 11
        self.channel_id = 22
        self.user = Named(id=user_id)
        self.sent: list[tuple[str, str | None, dict[str, Any]]] = []
        self.edited: list[dict[str, Any]] = []
        outer = self

        class _Response:
            def is_done(self) -> bool:
                return done

            async def send_message(self, content: str | None, **kwargs: Any) -> None:
                outer.sent.append(("response", content, kwargs))

            async def edit_message(self, **kwargs: Any) -> None:
                outer.edited.append(kwargs)

        class _Followup:
            async def send(self, content: str | None = None, **kwargs: Any) -> None:
                outer.sent.append(("followup", content, kwargs))

        self.response = _Response()
        self.followup = _Followup()


def stub_command(name: str, description: str = "", hidden: bool = False) -> Any:
    """Return a fake ``commands.Command`` as yielded by ``walk_commands``."""
    return Named(qualified_name=name, description=description, hidden=hidden)


def as_bot(stub: StubBot) -> commands.Bot:
    return cast("commands.Bot", stub)


def as_context(stub: StubContext) -> commands.Context[commands.Bot]:
    return cast("commands.Context[commands.Bot]", stub)


def as_interaction(stub: StubInteraction) -> discord.Interaction:
    return cast("discord.Interaction", stub)


def as_tree_interaction(stub: StubInteraction) -> discord.Interaction[AhaBot]:
    return cast("discord.Interaction[AhaBot]", stub)


def http_error() -> discord.HTTPException:
    """Build a real ``HTTPException`` without a live response object."""
    return discord.HTTPException(cast("Any", Named(status=500, reason="boom")), "boom")
