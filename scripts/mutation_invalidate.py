#!/usr/bin/env python3
"""Prepare a cached mutmut campaign for an incremental run (CI on pull requests).

CI restores ``mutants/`` from the last full campaign on ``main``. mutmut 3.8 then re-tests only the
mutants of functions whose code changed (and their callers, ``track_dependencies``), and with
``on_dependency_change = "rerun"`` it starts over when a non-Python file it watches changes
(``uv.lock``, ``pyproject.toml``, the locales). What it cannot see, and this script handles:

* **Edited or deleted tests.** A weakened assertion must not keep a cached kill. Every mutant of a
  function covered by a test in a changed test file is reset, so mutmut runs it again.
* **New tests.** A new test can kill a mutant that survived, so every mutant that was not killed
  (survived, no tests, suspicious, ...) is reset. There are few: the campaign scores about 100%.
* **Shared test code** (``conftest.py``, ``helpers.py``, ``__init__.py``) can change any test, so a
  change there deletes the cache and the run is a full one.
* **Stale copies.** mutmut copies ``tests/`` and the ``also_copy`` directories over the old ones but
  never deletes, so a removed test file would still run. Those copies are deleted (mutmut copies
  them again), and so are copied source files that no longer exist.

The cache records the commit it was built from (``git_commit`` in ``mutmut-stats.json``); the
change is ``git diff`` from there to the working tree. If that commit is unknown the cache is
deleted. Without a cache this does nothing, and ``mutmut run`` does a full campaign.

Usage: ``uv run python scripts/mutation_invalidate.py`` (then ``uv run mutmut run``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

MUTANTS = Path("mutants")
STATS = MUTANTS / "mutmut-stats.json"

#: Test code every test can depend on: a change there invalidates the whole cache.
SHARED_TEST_CODE = frozenset({"tests/conftest.py", "tests/helpers.py", "tests/__init__.py"})

#: Copied trees mutmut refreshes without deleting (``tests/`` plus ``also_copy`` in pyproject.toml).
COPIED_TREES = ("tests", "scripts", "docs")

#: Exit codes of a verdict that stands (mutmut.stats.status_by_exit_code): killed, timeout, caught
#: by the type checker, skipped. Anything else is tested again.
FINAL_EXIT_CODES = frozenset({1, 3, 24, -24, 36, 152, 255, 34, 37})

Git = Callable[[list[str]], str | None]


def run_git(args: list[str]) -> str | None:
    """``git`` output, or ``None`` when it fails (unknown commit, not a repository)."""
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def changed_paths(base: str, git: Git) -> set[str] | None:
    """Files changed from ``base`` to the working tree, including untracked ones."""
    diff = git(["diff", "--name-only", base, "--"])
    if diff is None:
        return None
    untracked = git(["ls-files", "--others", "--exclude-standard"]) or ""
    return {line for line in (diff + "\n" + untracked).splitlines() if line}


def functions_covered_by(stats: dict[str, Any], test_files: Iterable[str]) -> set[str]:
    """Mangled function names that any test in ``test_files`` covers."""
    files = set(test_files)
    return {
        function
        for function, tests in stats["tests_by_mangled_function_name"].items()
        if any(test.partition("::")[0] in files for test in tests)
    }


def reset_verdicts(meta_path: Path, functions: set[str]) -> int:
    """Clear the verdicts in one ``.meta`` file that must be tested again; returns how many."""
    meta = json.loads(meta_path.read_text())
    codes: dict[str, int | None] = meta["exit_code_by_key"]
    reset = 0
    for key, code in codes.items():
        if code is None:
            continue
        if code not in FINAL_EXIT_CODES or key.partition("__mutmut_")[0] in functions:
            codes[key] = None
            reset += 1
    if reset:
        meta_path.write_text(json.dumps(meta, indent=4))
    return reset


def prune_copies(root: Path) -> int:
    """Delete the refreshed copies, and copied sources that no longer exist; returns how many."""
    for tree in COPIED_TREES:
        shutil.rmtree(root / MUTANTS / tree, ignore_errors=True)
    removed = 0
    copied_src = root / MUTANTS / "src"
    if copied_src.is_dir():
        for path in sorted(copied_src.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:  # bytecode, not a source
                continue
            relative = path.relative_to(root / MUTANTS)
            original = relative.with_suffix("") if relative.suffix == ".meta" else relative
            if not (root / original).exists():
                path.unlink()
                removed += 1
    return removed


def prepare(root: Path, git: Git = run_git) -> str:
    """Make the cache under ``root`` safe for an incremental run; returns what was done."""
    stats_path = root / STATS
    if not stats_path.exists():
        return "no cached campaign: mutmut runs the full campaign"
    stats = json.loads(stats_path.read_text())
    base = stats.get("git_commit")
    paths = None if not base else changed_paths(base, git)
    if paths is None:
        shutil.rmtree(root / MUTANTS)
        return "cannot tell what changed since the cached campaign: cache deleted, full run"
    shared = sorted(paths & SHARED_TEST_CODE)
    if shared:
        shutil.rmtree(root / MUTANTS)
        return f"shared test code changed ({', '.join(shared)}): cache deleted, full run"
    tests = sorted(p for p in paths if p.startswith("tests/") and p.endswith(".py"))
    functions = functions_covered_by(stats, tests)
    reset = sum(
        reset_verdicts(meta, functions) for meta in sorted((root / MUTANTS).rglob("*.meta"))
    )
    removed = prune_copies(root)
    return (
        f"cached campaign from {base[:7]}: {len(tests)} changed test file(s) cover "
        f"{len(functions)} function(s); {reset} verdict(s) reset, {removed} stale file(s) removed"
    )


def main() -> int:
    print(prepare(Path.cwd()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
