"""Project-local command decorators.

``discord.ext.commands.hybrid_command`` is typed to accept ``CommandCallback``,
a ``Union`` of two ``Callable[Concatenate[..., ParamSpec], Coro]`` forms. mypy
cannot solve those type variables when the decorator is applied to a Cog
method, so it collapses the expected parameters to ``Never`` and rejects the
runtime-valid call::

    error: Argument 1 has incompatible type "Callable[[Meta, Context[Bot]], ...]";
    expected "def (Never, Never, /, *Never, **Never) -> ..."  [arg-type]

Applying the decorator through this wrapper keeps that single workaround in one
place, so every hybrid command in :mod:`aha.cogs` stays type-checked instead of
each call site carrying its own ``# type: ignore``. It is a drop-in replacement
for :func:`discord.ext.commands.hybrid_command`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from discord.ext import commands
from discord.utils import MISSING

__all__ = ["hybrid_command"]

F = TypeVar("F", bound=Callable[..., Any])


def hybrid_command(name: Any = MISSING, **attrs: Any) -> Callable[[F], F]:
    """Return a decorator that turns ``func`` into a hybrid command.

    mypy-safe stand-in for :func:`discord.ext.commands.hybrid_command`. The
    decorated function keeps its own type, so the callback's ``self`` and
    ``ctx`` annotations are still checked.
    """
    return commands.hybrid_command(name=name, **attrs)  # type: ignore[return-value]
