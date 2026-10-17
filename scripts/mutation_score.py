#!/usr/bin/env python3
"""Mutation-score gate for CI.

``mutmut run`` records per-mutant outcomes in its cache; ``mutmut
export-cicd-stats`` serialises the aggregate counts to
``mutants/mutmut-cicd-stats.json``. This gate reads that file and fails when the
mutation score falls below ``THRESHOLD``.

    score = (killed + timeout) / (total - skipped)

Accounting rules, and why:

* ``timeout`` counts as a **kill**. mutmut only reports ``timeout`` when a
  mutant's own test run overruns its per-mutant limit and the worker is killed
  with SIGXCPU (``mutmut.stats.status_by_exit_code`` maps exit codes 36/24/
  -24/152/255 to ``timeout``), i.e. the mutant hung the suite and was therefore
  detected. This matches mutmut's own CI/CD badge formula,
  ``(killed + timeout) / (total - skipped)``. A *runner or infrastructure*
  timeout never reaches this bucket: it aborts the campaign instead, which
  surfaces below as interrupted/unclassified mutants.
* ``no_tests``, ``suspicious`` and ``segfault`` are **not** kills and stay in
  the denominator, so untested or unclassified code lowers the score.
* Only ``skipped`` mutants are excluded (mutmut never ran them), and
  ``MAX_EXCLUDED_RATIO`` caps how many may be excluded before the campaign is
  rejected outright.
* An interrupted or incomplete campaign cannot score at all: any
  ``check_was_interrupted_by_user`` or unclassified mutant fails the gate, so a
  partial run can never be reported as passing.

Usage::

    uv run mutmut run
    uv run mutmut export-cicd-stats
    python scripts/mutation_score.py

Exit codes: 0 = threshold met, 1 = below threshold or invalid campaign,
2 = stats unavailable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

STATS_PATH = Path("mutants/mutmut-cicd-stats.json")

# Ratchet: the campaign currently scores ~66% (125/189 measured locally with
# mutmut 3.8.0). Keep the gate a few points below that so refactors have room
# while a real regression still trips it. Raise this as the surviving-mutant
# backlog is worked down.
THRESHOLD = 0.60

# At most this fraction of enumerated mutants may be excluded (skipped) before
# the campaign is considered unrepresentative and rejected.
MAX_EXCLUDED_RATIO = 0.10

# Outcomes that are not kills but are legitimately part of the denominator.
_NON_KILLING_KEYS = ("survived", "no_tests", "suspicious", "segfault")
# Outcomes excluded from the denominator, subject to MAX_EXCLUDED_RATIO.
_EXCLUDED_KEYS = ("skipped",)
# Outcomes that mean the campaign itself is invalid, not merely low-scoring.
_INVALID_RUN_KEYS = ("check_was_interrupted_by_user",)


def _count(stats: dict[str, Any], key: str) -> int:
    """Return a non-negative integer count for ``key`` (absent key -> 0)."""
    value = stats.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key!r} is not an integer count: {value!r}")
    if value < 0:
        raise ValueError(f"{key!r} is negative: {value}")
    return value


def mutation_score(stats: dict[str, Any]) -> tuple[float, int, int]:
    """Return ``(score, killed, scored)`` for an exported stats mapping.

    ``scored`` is the denominator (mutants that count towards the score) and
    ``killed`` the numerator (kills plus mutant-induced timeouts). Raises
    ``ValueError`` when the campaign is not scorable.
    """
    total = _count(stats, "total")
    if total <= 0:
        raise ValueError("no mutants were enumerated")

    excluded = sum(_count(stats, key) for key in _EXCLUDED_KEYS)
    scored = total - excluded
    if scored <= 0:
        raise ValueError(f"no scored mutants ({excluded} of {total} excluded)")
    if excluded / total > MAX_EXCLUDED_RATIO:
        raise ValueError(
            f"too many excluded mutants: {excluded}/{total} "
            f"({excluded / total:.1%}) exceeds the {MAX_EXCLUDED_RATIO:.0%} limit"
        )

    accounted = (
        excluded
        + _count(stats, "killed")
        + sum(_count(stats, key) for key in _NON_KILLING_KEYS)
        + sum(_count(stats, key) for key in _INVALID_RUN_KEYS)
        + _count(stats, "timeout")
    )
    unclassified = total - accounted

    problems = [
        f"{key}={_count(stats, key)}" for key in _INVALID_RUN_KEYS if _count(stats, key) > 0
    ]
    if unclassified:
        problems.append(f"unclassified mutants={unclassified} (campaign incomplete)")
    if problems:
        raise ValueError("invalid mutation campaign: " + ", ".join(problems))

    killed = _count(stats, "killed") + _count(stats, "timeout")
    return killed / scored, killed, scored


def describe(stats: dict[str, Any], score: float, killed: int, scored: int) -> str:
    """Return the one-line human-readable summary of the campaign."""
    details = " ".join(f"{key}={_count(stats, key)}" for key in ("survived", "timeout", "no_tests"))
    return (
        f"mutation score {score:.2%} (killed={killed} {details} "
        f"scored={scored} total={_count(stats, 'total')})"
    )


def main() -> int:
    if not STATS_PATH.exists():
        print(
            f"error: {STATS_PATH} not found — run `mutmut run` first",
            file=sys.stderr,
        )
        return 2

    try:
        stats: dict[str, Any] = json.loads(STATS_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: could not read {STATS_PATH}: {exc}", file=sys.stderr)
        return 2

    try:
        score, killed, scored = mutation_score(stats)
        summary = describe(stats, score, killed, scored)
    except (KeyError, TypeError, ValueError) as exc:
        print(f"error: could not score mutation stats: {exc}", file=sys.stderr)
        return 1

    print(summary)
    if score < THRESHOLD:
        print(f"FAIL: {score:.2%} is below the {THRESHOLD:.0%} threshold")
        return 1
    print(f"OK: meets the {THRESHOLD:.0%} threshold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
