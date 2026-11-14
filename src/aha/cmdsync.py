"""Compare the local application-command tree with what Discord currently has.

A command sync is a bulk overwrite, so doing it on every start is wasteful and, with more than
one revision running, a race (the last writer wins). This module answers one question: *would a
sync change anything?* It is deliberately conservative. Everything Discord stores about a command
is compared, and only values that Discord documents as defaults are treated as equal to "not
set". When the comparison is unsure it reports a change: a redundant sync costs one REST write,
while a wrongly skipped sync leaves users with stale commands.

The functions here are pure and work on plain payload dicts;
:meth:`aha.bot.AhaBot.sync_commands` does the I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["CommandDiff", "diff_commands", "local_signature", "remote_signature"]

#: ``contexts`` Discord assumes when a command does not set any: guild, bot DM, private channel.
_DEFAULT_CONTEXTS = [0, 1, 2]

#: Contexts of a command that is guild-only (the legacy ``dm_permission=False``).
_GUILD_ONLY_CONTEXTS = [0]

#: ``AppCommandType.chat_input``; other types are context menus, which are labelled as such.
_CHAT_INPUT = 1


@dataclass(frozen=True, slots=True)
class CommandDiff:
    """The difference a sync would make, as sorted command labels."""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()

    @property
    def unchanged(self) -> bool:
        """True when local and remote already agree."""
        return not (self.added or self.removed or self.changed)

    def lines(self) -> list[str]:
        """Return ``+ name`` / ``- name`` / ``~ name`` lines, one per difference."""
        return [
            *(f"+ {name}" for name in self.added),
            *(f"- {name}" for name in self.removed),
            *(f"~ {name}" for name in self.changed),
        ]


def _is_empty(value: object) -> bool:
    """Return True for values Discord treats as "not set" (``None``, ``False``, empty)."""
    # ``0`` must stay meaningful (``min_value=0``), so test for False by identity.
    return value is None or value is False or value == [] or value == {}


def _prune(value: Any) -> Any:
    """Drop unset values recursively so ``required: False`` equals a missing key."""
    if isinstance(value, Mapping):
        return {
            key: _prune(item) for key, item in sorted(value.items()) if not _is_empty(_prune(item))
        }
    if isinstance(value, (list, tuple)):
        return [_prune(item) for item in value]
    return value


def _contexts(contexts: Iterable[int] | None, dm_permission: bool) -> list[int]:
    if contexts is not None:
        return sorted(contexts)
    return list(_DEFAULT_CONTEXTS if dm_permission else _GUILD_ONLY_CONTEXTS)


def _installs(installs: Iterable[int] | None) -> list[int] | None:
    """The install contexts, or ``None`` when the command does not say.

    A command that does not declare them gets whatever the application allows in the Developer
    Portal (which can include user installs), so the comparison must not assume a default.
    """
    return None if installs is None else sorted(installs)


def local_signature(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a local command (the dict from ``Command.to_dict(tree)``)."""
    return {
        "name": payload["name"],
        "description": payload.get("description", ""),
        "type": payload.get("type", _CHAT_INPUT),
        "options": _prune(payload.get("options") or []),
        "nsfw": bool(payload.get("nsfw")),
        "default_member_permissions": payload.get("default_member_permissions"),
        "contexts": _contexts(payload.get("contexts"), payload.get("dm_permission") is not False),
        "integration_types": _installs(payload.get("integration_types")),
        "name_localizations": _prune(payload.get("name_localizations") or {}),
        "description_localizations": _prune(payload.get("description_localizations") or {}),
    }


def remote_signature(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a command as Discord returned it (a raw payload), like :func:`local_signature`.

    Raw payloads are compared rather than :class:`discord.app_commands.AppCommand` objects
    because discord.py 2.7 parses ``contexts`` and ``integration_types`` lossily (it drops the
    private-channel context and the guild install), which would make a changed command look
    unchanged.
    """
    permissions = payload.get("default_member_permissions")
    return {
        "name": payload["name"],
        "description": payload.get("description", ""),
        "type": payload.get("type", _CHAT_INPUT),
        "options": _prune(payload.get("options") or []),
        "nsfw": bool(payload.get("nsfw")),
        # Discord sends the permission bitfield as a string; the local tree uses an int.
        "default_member_permissions": None if permissions is None else int(permissions),
        "contexts": _contexts(payload.get("contexts"), payload.get("dm_permission") is not False),
        "integration_types": _installs(payload.get("integration_types")),
        "name_localizations": _prune(payload.get("name_localizations") or {}),
        "description_localizations": _prune(payload.get("description_localizations") or {}),
    }


def _label(signature: Mapping[str, Any]) -> str:
    name = str(signature["name"])
    return name if signature["type"] == _CHAT_INPUT else f"{name} (context menu)"


def _key(signature: Mapping[str, Any]) -> tuple[int, str]:
    return (signature["type"], signature["name"])


def diff_commands(
    local: Iterable[Mapping[str, Any]], remote: Iterable[Mapping[str, Any]]
) -> CommandDiff:
    """Return what syncing ``local`` over ``remote`` would add, remove and change.

    Commands are matched by ``(type, name)``; a matched pair counts as changed when any
    compared field differs.
    """
    local_by_key = {_key(sig): sig for sig in map(local_signature, local)}
    remote_by_key = {_key(sig): sig for sig in map(remote_signature, remote)}
    for key, sig in local_by_key.items():
        # Undeclared locally means "the application's default": whatever Discord holds is right.
        if sig["integration_types"] is None and key in remote_by_key:
            remote_by_key[key] = {**remote_by_key[key], "integration_types": None}
    added = sorted(_label(local_by_key[key]) for key in local_by_key.keys() - remote_by_key.keys())
    removed = sorted(
        _label(remote_by_key[key]) for key in remote_by_key.keys() - local_by_key.keys()
    )
    changed = sorted(
        _label(local_by_key[key])
        for key in local_by_key.keys() & remote_by_key.keys()
        if local_by_key[key] != remote_by_key[key]
    )
    return CommandDiff(tuple(added), tuple(removed), tuple(changed))
