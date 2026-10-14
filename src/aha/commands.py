"""Project-local command decorators.

``discord.ext.commands.hybrid_command`` is typed to accept ``CommandCallback``,
a ``Union`` of two ``Callable[Concatenate[..., ParamSpec], Coro]`` forms. mypy
cannot solve those type variables when the decorator is applied to a Cog
method, so it collapses the expected parameters to ``Never`` and rejects the
runtime-valid call::

    error: Argument 1 has incompatible type "Callable[[Meta, Context[Bot]], ...]";
    expected "def (Never, Never, /, *Never, **Never) -> ..."  [arg-type]

Applying the decorator through this wrapper fixes it once: the wrapper accepts
any callback and returns the real ``HybridCommand`` type, so no call site — and
no line here — needs a ``# type: ignore``, and every hybrid command in
:mod:`aha.cogs` stays fully type-checked. It is a drop-in replacement for
:func:`discord.ext.commands.hybrid_command`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from discord.ext import commands
from discord.utils import MISSING

__all__ = ["hybrid_command"]

F = TypeVar("F", bound=Callable[..., Any])


def hybrid_command(
    name: Any = MISSING,
    **attrs: Any,
) -> Callable[[F], commands.HybridCommand[Any, ..., Any]]:
    """Return a decorator that turns ``func`` into a hybrid command.

    mypy-safe stand-in for :func:`discord.ext.commands.hybrid_command`. The
    callback's own ``self``/``ctx`` annotations are still checked; only the
    decorator's return type is widened.
    """
    return commands.hybrid_command(name=name, **attrs)
