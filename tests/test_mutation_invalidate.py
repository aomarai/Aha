"""Tests for the incremental mutation cache step (``scripts/mutation_invalidate.py``)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import mutation_invalidate as inv

VOICE = "aha.cogs.voice.xǁVoiceǁsweep"
JOIN = "aha.cogs.voice.xǁVoiceǁjoin"
BOT = "aha.bot.x_gateway_intents"


def cache(root: Path, *, commit: str | None = "abc1234def", codes: dict[str, Any]) -> Path:
    """A minimal mutmut cache: stats with test coverage, one .meta file, copied trees."""
    stats = {
        "git_commit": commit,
        "tests_by_mangled_function_name": {
            VOICE: ["tests/test_voice_cog.py::test_sweep"],
            JOIN: ["tests/test_voice_cog.py::test_join", "tests/test_bot.py::test_x"],
            BOT: ["tests/test_bot.py::test_intents"],
        },
    }
    (root / "mutants" / "src" / "aha" / "cogs").mkdir(parents=True)
    (root / "mutants" / "mutmut-stats.json").write_text(json.dumps(stats))
    meta = root / "mutants" / "src" / "aha" / "cogs" / "voice.py.meta"
    meta.write_text(json.dumps({"exit_code_by_key": codes, "hash_by_function_name": {"f": "1"}}))
    (root / "mutants" / "src" / "aha" / "cogs" / "voice.py").write_text("# mutated copy")
    (root / "src" / "aha" / "cogs").mkdir(parents=True)
    (root / "src" / "aha" / "cogs" / "voice.py").write_text("# source")
    for tree in inv.COPIED_TREES:
        (root / "mutants" / tree).mkdir()
        (root / "mutants" / tree / "stale.py").write_text("")
    return meta


def git_reporting(diff: str | None, untracked: str = "") -> tuple[inv.Git, list[list[str]]]:
    calls: list[list[str]] = []

    def git(args: list[str]) -> str | None:
        calls.append(args)
        return diff if args[0] == "diff" else untracked

    return git, calls


def codes_of(meta: Path) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(meta.read_text())["exit_code_by_key"]
    return result


def test_without_a_cache_nothing_happens(tmp_path: Path) -> None:
    git, calls = git_reporting("")

    assert inv.prepare(tmp_path, git) == "no cached campaign: mutmut runs the full campaign"
    assert calls == []


def test_mutants_covered_by_a_changed_test_file_and_every_unkilled_mutant_are_reset(
    tmp_path: Path,
) -> None:
    meta = cache(
        tmp_path,
        codes={
            f"{VOICE}__mutmut_1": 1,  # killed, covered by the changed file: reset
            f"{JOIN}__mutmut_1": 1,  # killed, one of its tests is in the changed file: reset
            f"{BOT}__mutmut_1": 1,  # killed, not covered: kept
            f"{BOT}__mutmut_2": 0,  # survived: reset (a new test may kill it)
            f"{BOT}__mutmut_3": 33,  # no tests: reset
            f"{BOT}__mutmut_4": 36,  # timeout counts as a kill: kept
            f"{BOT}__mutmut_5": 37,  # caught by the type checker: kept
            f"{BOT}__mutmut_6": None,  # not checked yet: left alone
        },
    )
    git, calls = git_reporting("tests/test_voice_cog.py\nsrc/aha/cogs/voice.py\nREADME.md\n")

    message = inv.prepare(tmp_path, git)

    assert calls == [
        ["diff", "--name-only", "abc1234def", "--"],
        ["ls-files", "--others", "--exclude-standard"],
    ]
    assert codes_of(meta) == {
        f"{VOICE}__mutmut_1": None,
        f"{JOIN}__mutmut_1": None,
        f"{BOT}__mutmut_1": 1,
        f"{BOT}__mutmut_2": None,
        f"{BOT}__mutmut_3": None,
        f"{BOT}__mutmut_4": 36,
        f"{BOT}__mutmut_5": 37,
        f"{BOT}__mutmut_6": None,
    }
    assert json.loads(meta.read_text())["hash_by_function_name"] == {"f": "1"}  # kept as is
    assert message == (
        "cached campaign from abc1234: 1 changed test file(s) cover 2 function(s); "
        "4 verdict(s) reset, 0 stale file(s) removed"
    )


def test_an_untracked_test_file_counts_as_changed(tmp_path: Path) -> None:
    meta = cache(tmp_path, codes={f"{BOT}__mutmut_1": 1, f"{VOICE}__mutmut_1": 1})
    git, _ = git_reporting("", untracked="tests/test_bot.py\n")

    inv.prepare(tmp_path, git)

    assert codes_of(meta) == {f"{BOT}__mutmut_1": None, f"{VOICE}__mutmut_1": 1}


def test_an_untouched_meta_file_is_not_rewritten(tmp_path: Path) -> None:
    meta = cache(tmp_path, codes={f"{BOT}__mutmut_1": 1})
    meta.write_text(meta.read_text() + "\n")  # a byte rewriting would drop
    before = meta.read_text()
    git, _ = git_reporting("src/aha/bot.py\n")

    inv.prepare(tmp_path, git)

    assert meta.read_text() == before


@pytest.mark.parametrize("shared", sorted(inv.SHARED_TEST_CODE))
def test_a_change_to_shared_test_code_deletes_the_cache(tmp_path: Path, shared: str) -> None:
    cache(tmp_path, codes={f"{BOT}__mutmut_1": 1})
    git, _ = git_reporting(f"tests/test_bot.py\n{shared}\n")

    message = inv.prepare(tmp_path, git)

    assert not (tmp_path / "mutants").exists()
    assert message == f"shared test code changed ({shared}): cache deleted, full run"


@pytest.mark.parametrize("commit", [None, ""])
def test_a_cache_without_its_commit_is_deleted(tmp_path: Path, commit: str | None) -> None:
    cache(tmp_path, commit=commit, codes={})
    git, calls = git_reporting("")

    message = inv.prepare(tmp_path, git)

    assert calls == []
    assert not (tmp_path / "mutants").exists()
    assert message == (
        "cannot tell what changed since the cached campaign: cache deleted, full run"
    )


def test_a_commit_git_does_not_know_deletes_the_cache(tmp_path: Path) -> None:
    cache(tmp_path, codes={})
    git, _ = git_reporting(None)

    inv.prepare(tmp_path, git)

    assert not (tmp_path / "mutants").exists()


def test_stale_copies_are_removed_and_current_sources_kept(tmp_path: Path) -> None:
    meta = cache(tmp_path, codes={})
    gone = tmp_path / "mutants" / "src" / "aha" / "removed.py"
    gone.write_text("")
    gone_meta = tmp_path / "mutants" / "src" / "aha" / "removed.py.meta"
    gone_meta.write_text(json.dumps({"exit_code_by_key": {}}))
    bytecode = tmp_path / "mutants" / "src" / "aha" / "__pycache__" / "removed.cpython-314.pyc"
    bytecode.parent.mkdir()
    bytecode.write_text("")
    git, _ = git_reporting("")

    message = inv.prepare(tmp_path, git)

    for tree in inv.COPIED_TREES:
        assert not (tmp_path / "mutants" / tree).exists()
    assert not gone.exists() and not gone_meta.exists()
    assert bytecode.exists()  # not a source: Python manages it
    assert meta.exists()
    assert (tmp_path / "mutants" / "src" / "aha" / "cogs" / "voice.py").exists()
    assert (tmp_path / "mutants" / "mutmut-stats.json").exists()
    assert message.endswith("0 verdict(s) reset, 2 stale file(s) removed")


def test_run_git_returns_output_or_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert inv.run_git(["--version"]) is not None and "git" in str(inv.run_git(["--version"]))
    assert inv.run_git(["rev-parse", "HEAD"]) is None  # not a repository

    def missing(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing)
    assert inv.run_git(["--version"]) is None


def test_main_prints_what_it_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert inv.main() == 0
    assert capsys.readouterr().out == "no cached campaign: mutmut runs the full campaign\n"
