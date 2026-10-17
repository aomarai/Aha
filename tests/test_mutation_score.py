"""Tests for the CI mutation-score gate (``scripts/mutation_score.py``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from scripts import mutation_score as gate


def stats(**counts: int) -> dict[str, Any]:
    """Build an exported ``mutmut-cicd-stats.json`` mapping from key counts."""
    return dict(counts)


def test_clean_campaign_scores_kills_over_unskipped() -> None:
    score, killed, scored = gate.mutation_score(
        stats(total=100, killed=70, survived=25, no_tests=3, suspicious=1, timeout=1)
    )
    assert killed == 71
    assert scored == 100
    assert score == pytest.approx(0.71)


def test_timeout_counts_as_a_kill_not_an_exclusion() -> None:
    """A hanging mutant was detected, so it is a kill (mutmut runs no infra timeouts)."""
    score, killed, scored = gate.mutation_score(stats(total=10, killed=5, timeout=5))
    assert killed == 10
    assert scored == 10
    assert score == pytest.approx(1.0)


def test_untested_mutants_lower_the_score() -> None:
    """``no_tests`` must stay in the denominator: untested code cannot score 100%."""
    score, killed, scored = gate.mutation_score(stats(total=100, killed=60, no_tests=40))
    assert killed == 60
    assert scored == 100
    assert score == pytest.approx(0.60)
    assert score >= gate.THRESHOLD


def test_surviving_untested_mutants_fail_the_threshold() -> None:
    score, _, _ = gate.mutation_score(stats(total=100, killed=50, no_tests=50))
    assert score < gate.THRESHOLD


def test_skipped_mutants_are_excluded_within_the_ratio_limit() -> None:
    score, killed, scored = gate.mutation_score(
        stats(total=100, killed=60, survived=30, skipped=10)
    )
    assert (killed, scored) == (60, 90)
    assert score == pytest.approx(60 / 90)


def test_excessive_exclusions_are_rejected() -> None:
    with pytest.raises(ValueError, match="too many excluded mutants"):
        gate.mutation_score(stats(total=100, killed=55, survived=34, skipped=11))


def test_everything_excluded_is_rejected() -> None:
    with pytest.raises(ValueError, match="no scored mutants"):
        gate.mutation_score(stats(total=10, skipped=10))


def test_interrupted_campaign_is_rejected() -> None:
    with pytest.raises(ValueError, match="check_was_interrupted_by_user=1"):
        gate.mutation_score(stats(total=10, killed=8, survived=1, check_was_interrupted_by_user=1))


def test_unclassified_mutants_are_rejected() -> None:
    """A partial campaign (mutants with no recorded verdict) must not score."""
    with pytest.raises(ValueError, match="unclassified mutants=2"):
        gate.mutation_score(stats(total=10, killed=8))


def test_no_mutants_is_rejected() -> None:
    with pytest.raises(ValueError, match="no mutants were enumerated"):
        gate.mutation_score(stats(total=0))


def test_non_integer_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="not an integer count"):
        gate.mutation_score({"total": 1, "killed": "8"})


def write_stats(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path


def test_main_passes_at_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stats_path = write_stats(
        tmp_path / "mutants" / "mutmut-cicd-stats.json", stats(total=100, killed=70, survived=30)
    )
    monkeypatch.setattr(gate, "STATS_PATH", stats_path)
    assert gate.main() == 0
    assert "OK:" in capsys.readouterr().out


def test_main_fails_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stats_path = write_stats(
        tmp_path / "mutants" / "mutmut-cicd-stats.json", stats(total=100, killed=59, survived=41)
    )
    monkeypatch.setattr(gate, "STATS_PATH", stats_path)
    assert gate.main() == 1
    assert "FAIL:" in capsys.readouterr().out


def test_main_fails_on_invalid_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stats_path = write_stats(
        tmp_path / "mutants" / "mutmut-cicd-stats.json", stats(total=10, skipped=5, killed=5)
    )
    monkeypatch.setattr(gate, "STATS_PATH", stats_path)
    assert gate.main() == 1
    assert "could not score mutation stats" in capsys.readouterr().err


def test_main_reports_missing_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gate, "STATS_PATH", tmp_path / "mutants" / "mutmut-cicd-stats.json")
    assert gate.main() == 2
    assert "not found" in capsys.readouterr().err


def test_main_reports_malformed_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stats_path = tmp_path / "mutants" / "mutmut-cicd-stats.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text("{not json")
    monkeypatch.setattr(gate, "STATS_PATH", stats_path)
    assert gate.main() == 2
    assert "could not read" in capsys.readouterr().err
