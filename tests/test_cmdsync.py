"""Tests for the local-versus-remote command comparison."""

from __future__ import annotations

import copy
from typing import Any

from aha.cmdsync import CommandDiff, diff_commands, local_signature, remote_signature

#: What ``Command.to_dict(tree)`` returns for a simple guild-or-DM command.
_PING: dict[str, Any] = {
    "name": "ping",
    "description": "Check the bot's WebSocket latency.",
    "type": 1,
    "options": [],
    "nsfw": False,
    "dm_permission": True,
    "default_member_permissions": None,
    "contexts": None,
    "integration_types": None,
}

#: A command with an option, as the local tree serialises it.
_ROLL: dict[str, Any] = {
    "name": "roll",
    "description": "Roll a die.",
    "type": 1,
    "options": [{"type": 4, "name": "sides", "description": "How many", "required": False}],
    "nsfw": False,
    "dm_permission": True,
    "default_member_permissions": None,
    "contexts": None,
    "integration_types": None,
}


def _remote(payload: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Build the raw payload Discord would return for ``payload`` (plus its own fields)."""
    data = copy.deepcopy(payload)
    permissions = data.get("default_member_permissions")
    if permissions is not None:
        data["default_member_permissions"] = str(permissions)  # Discord sends it as a string
    data.update({"id": "1", "application_id": "2", "version": "3", **overrides})
    for option in data.get("options", []):
        option.setdefault("name_localizations", None)  # Discord echoes null localizations
    return data


def _diff(local: list[dict[str, Any]], remote: list[dict[str, Any]]) -> CommandDiff:
    return diff_commands(local, remote)


def test_identical_trees_are_unchanged() -> None:
    diff = _diff([_PING, _ROLL], [_remote(_PING), _remote(_ROLL)])

    assert diff.unchanged is True
    assert diff.lines() == []


def test_a_new_local_command_is_reported_as_added() -> None:
    diff = _diff([_PING, _ROLL], [_remote(_PING)])

    assert diff == CommandDiff(added=("roll",))
    assert diff.lines() == ["+ roll"]


def test_a_command_missing_locally_is_reported_as_removed() -> None:
    diff = _diff([_PING], [_remote(_PING), _remote(_ROLL)])

    assert diff == CommandDiff(removed=("roll",))
    assert diff.lines() == ["- roll"]


def test_changes_are_reported_per_command_and_lines_are_ordered() -> None:
    changed = {**_PING, "description": "Different."}

    diff = _diff(
        [changed, _ROLL], [_remote(_PING), _remote(_ROLL), _remote({**_PING, "name": "z"})]
    )

    assert diff == CommandDiff(removed=("z",), changed=("ping",))
    assert diff.lines() == ["- z", "~ ping"]


def test_an_empty_local_tree_removes_everything() -> None:
    assert _diff([], [_remote(_PING)]) == CommandDiff(removed=("ping",))


def test_two_empty_trees_are_unchanged() -> None:
    assert _diff([], []).unchanged is True


def test_a_changed_option_is_a_change() -> None:
    other = {**_ROLL, "options": [{**_ROLL["options"][0], "description": "Changed"}]}

    assert _diff([other], [_remote(_ROLL)]) == CommandDiff(changed=("roll",))


def test_option_required_false_equals_a_missing_key() -> None:
    remote_payload = copy.deepcopy(_ROLL)
    del remote_payload["options"][0]["required"]

    assert _diff([_ROLL], [_remote(remote_payload)]).unchanged is True


def test_option_required_true_differs_from_false() -> None:
    required = {**_ROLL, "options": [{**_ROLL["options"][0], "required": True}]}

    assert _diff([required], [_remote(_ROLL)]) == CommandDiff(changed=("roll",))


def test_a_zero_min_value_is_not_treated_as_unset() -> None:
    with_min = {**_ROLL, "options": [{**_ROLL["options"][0], "min_value": 0}]}

    assert _diff([with_min], [_remote(_ROLL)]) == CommandDiff(changed=("roll",))
    assert _diff([with_min], [_remote(with_min)]).unchanged is True


def test_nsfw_default_member_permissions_and_localizations_are_compared() -> None:
    for field, value in (
        ("nsfw", True),
        ("default_member_permissions", 32),
        ("name_localizations", {"de": "ping-de"}),
        ("description_localizations", {"de": "Beschreibung"}),
    ):
        local = {**_PING, field: value}
        remote = _remote(_PING)

        assert _diff([local], [remote]) == CommandDiff(changed=("ping",)), field
        assert _diff([local], [_remote(local)]).unchanged is True, field


def test_default_contexts_equal_unset_ones_and_undeclared_installs_follow_discord() -> None:
    # A command that does not declare install contexts gets the application's configured ones
    # (live: [0, 1]), so whatever Discord holds is not a difference.
    for installs in ([0], [1], [0, 1]):
        remote = _remote(_PING, contexts=[0, 1, 2], integration_types=installs)
        assert _diff([_PING], [remote]).unchanged is True, installs


def test_non_default_contexts_and_declared_installs_are_changes() -> None:
    assert _diff([_PING], [_remote(_PING, contexts=[0])]) == CommandDiff(changed=("ping",))
    declared = {**_PING, "integration_types": [0]}
    assert _diff([declared], [_remote(_PING, integration_types=[0, 1])]) == CommandDiff(
        changed=("ping",)
    )
    assert _diff([declared], [_remote(declared)]).unchanged is True
    explicit = {**_PING, "contexts": [0, 1], "integration_types": [1]}
    assert _diff([explicit], [_remote(_PING)]) == CommandDiff(changed=("ping",))
    assert _diff([explicit], [_remote(explicit)]).unchanged is True


def test_a_guild_only_command_matches_however_discord_reports_it() -> None:
    guild_only = {**_PING, "name": "sync", "dm_permission": False}

    assert _diff([guild_only], [_remote(guild_only)]).unchanged is True
    assert _diff([guild_only], [_remote(guild_only, dm_permission=None, contexts=[0])]).unchanged
    assert _diff([guild_only], [_remote(guild_only, dm_permission=None, contexts=None)]) == (
        CommandDiff(changed=("sync",))
    )
    # A command that allows DMs locally but is guild-only remotely has changed.
    assert _diff([{**guild_only, "dm_permission": True}], [_remote(guild_only)]) == CommandDiff(
        changed=("sync",)
    )


def test_context_menu_commands_are_matched_by_type_and_labelled() -> None:
    menu = {**_PING, "name": "Inspect", "type": 2, "description": ""}
    chat = {**_PING, "name": "Inspect"}

    diff = _diff([chat, menu], [_remote(chat)])

    assert diff == CommandDiff(added=("Inspect (context menu)",))
    assert _diff([chat], [_remote(chat), _remote(menu)]) == CommandDiff(
        removed=("Inspect (context menu)",)
    )


def test_local_signature_fills_defaults_for_a_minimal_payload() -> None:
    assert local_signature({"name": "x"}) == {
        "name": "x",
        "description": "",
        "type": 1,
        "options": [],
        "nsfw": False,
        "default_member_permissions": None,
        "contexts": [0, 1, 2],
        "integration_types": None,
        "name_localizations": {},
        "description_localizations": {},
    }


def test_the_private_channel_context_and_the_guild_install_are_not_lost() -> None:
    # discord.py's AppCommand parsing drops both; raw payloads must keep them.
    remote = _remote(_PING, contexts=[0, 1, 2], integration_types=[0, 1])

    assert _diff([{**_PING, "contexts": [0, 1]}], [remote]) == CommandDiff(changed=("ping",))
    assert _diff([{**_PING, "integration_types": [0]}], [remote]) == CommandDiff(changed=("ping",))
    full = {**_PING, "contexts": [0, 1, 2], "integration_types": [0, 1]}
    assert _diff([full], [_remote(full)]).unchanged is True


def test_remote_signature_reports_permissions_as_an_integer() -> None:
    signature = remote_signature(_remote({**_PING, "default_member_permissions": 32}))

    assert signature["default_member_permissions"] == 32
    assert signature["contexts"] == [0, 1, 2]
    assert signature["integration_types"] is None


def test_missing_keys_default_the_same_way_on_both_sides() -> None:
    # A context menu has no description; Discord sends "" and the local tree omits the key.
    local = {"name": "Inspect"}
    remote = {"id": "1", "application_id": "2", "version": "3", "name": "Inspect", "type": 1}

    assert _diff([local], [remote]).unchanged is True
    assert local_signature(local) == remote_signature(remote)


def test_a_remote_payload_without_optional_keys_matches_the_defaults() -> None:
    signature = remote_signature({"name": "x"})

    assert signature == {
        "name": "x",
        "description": "",
        "type": 1,
        "options": [],
        "nsfw": False,
        "default_member_permissions": None,
        "contexts": [0, 1, 2],
        "integration_types": None,
        "name_localizations": {},
        "description_localizations": {},
    }


def test_nsfw_defaults_to_false_when_absent_remotely() -> None:
    assert _diff([_PING], [{k: v for k, v in _remote(_PING).items() if k != "nsfw"}]).unchanged
    assert _diff(
        [{**_PING, "nsfw": True}],
        [{"name": "ping", "description": _PING["description"], "type": 1}],
    ) == (CommandDiff(changed=("ping",)))


def test_empty_lists_and_dicts_equal_missing_keys() -> None:
    remote_payload = copy.deepcopy(_ROLL)
    remote_payload["options"][0].update(
        {"choices": [], "channel_types": [], "name_localizations": {}, "autocomplete": False}
    )

    assert _diff([_ROLL], [_remote(remote_payload)]).unchanged is True


def test_a_non_empty_list_or_dict_is_a_change() -> None:
    with_choices = copy.deepcopy(_ROLL)
    with_choices["options"][0]["choices"] = [{"name": "one", "value": 1}]

    assert _diff([_ROLL], [_remote(with_choices)]) == CommandDiff(changed=("roll",))
    assert _diff([with_choices], [_remote(_ROLL)]) == CommandDiff(changed=("roll",))
