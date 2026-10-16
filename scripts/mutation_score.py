#!/usr/bin/env python3
"""Mutation-score gate for CI.

``mutmut run`` records per-mutant outcomes in its cache; ``mutmut
export-cicd-stats`` serialises the aggregate counts to
``mutants/mutmut-cicd-stats.json``. This gate reads that file and fails when the
mutation score falls below ``THRESHOLD``.

    score = killed / (total - timeout - skipped - no_tests)

Timeout, skipped and no-test mutants are removed from the denominator: a
timeout means the mutant hung the suite rather than being caught, and counting
it as a survivor would penalise the score for an infrastructure artefact.

Usage::

    uv run mutmut run
    uv run mutmut export-cicd-stats
    python scripts/mutation_score.py

Exit codes: 0 = threshold met, 1 = below threshold, 2 = stats unavailable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

STATS_PATH = Path("mutants/mutmut-cicd-stats.json")

# Ratchet: the campaign currently scores ~66% (125/188). Keep the gate a few
# points below that so refactors have room while a real regression still trips
# it. Raise this as the surviving-mutant backlog is worked down.
THRESHOLD = 0.60

_NON_SCORING_KEYS = ("timeout", "skipped", "no_tests")


def mutation_score(stats: dict[str, Any]) -> tuple[float, int]:
    """Return ``(score, viable)``, excluding non-scoring mutant outcomes."""
    excluded = sum(int(stats.get(key, 0)) for key in _NON_SCORING_KEYS)
    viable = int(stats["total"]) - excluded
    if viable <= 0:
        raise ValueError("no viable mutants to score")
    return int(stats["killed"]) / viable, viable


def main() -> int:
    if not STATS_PATH.exists():
        print(
            f"error: {STATS_PATH} not found — run `mutmut run` first",
            file=sys.stderr,
        )
        return 2

    stats: dict[str, Any] = json.loads(STATS_PATH.read_text())
    try:
        score, viable = mutation_score(stats)
    except (KeyError, ValueError) as exc:
        print(f"error: could not read mutation stats: {exc}", file=sys.stderr)
        return 2

    print(
        f"mutation score {score:.2%} "
        f"(killed={stats['killed']} survived={stats['survived']} "
        f"timeout={stats.get('timeout', 0)} viable={viable})"
    )
    if score < THRESHOLD:
        print(f"FAIL: {score:.2%} is below the {THRESHOLD:.0%} threshold")
        return 1
    print(f"OK: meets the {THRESHOLD:.0%} threshold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
