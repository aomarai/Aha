"""Tests for the shared text-neutralising helpers (also exports the zero-width space)."""

from __future__ import annotations

from aha.textutil import clip, code, plain

#: What discord.py inserts to defuse an ``@everyone`` / ``@here`` mention.
ZWSP = "​"


def test_plain_defuses_mentions_and_escapes_markdown() -> None:
    assert plain("@everyone and @here") == f"@{ZWSP}everyone and @{ZWSP}here"
    assert (
        plain("**bold** _it_ ~~x~~ ||spoiler||")
        == "\\*\\*bold\\*\\* \\_it\\_ \\~\\~x\\~\\~ \\|\\|spoiler\\|\\|"
    )
    assert plain("<@123456789012345678>") == f"<@{ZWSP}123456789012345678>"


def test_plain_turns_backticks_into_apostrophes_without_a_stray_backslash() -> None:
    assert plain("a`b```c") == "a'b'''c"


def test_plain_leaves_ordinary_text_alone() -> None:
    assert plain("Hello, world 123!") == "Hello, world 123!"
    assert plain("") == ""


def test_code_only_removes_backticks() -> None:
    assert code("a`b``c") == "a'b''c"
    assert code("Mars_Olympus*") == "Mars_Olympus*"  # a code span neutralises markdown itself


def test_clip_keeps_text_up_to_the_limit_and_marks_a_cut() -> None:
    assert clip("abcde", 5) == "abcde"
    assert clip("abcdef", 5) == "abcd…"
    assert clip("", 5) == ""
    assert len(clip("x" * 1000, 7)) == 7
