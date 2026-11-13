"""The version is declared twice (``pyproject.toml`` and ``aha.__version__``); keep them equal."""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

from aha import __version__

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_the_package_version_matches_pyproject() -> None:
    declared = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]

    assert __version__ == declared


def test_the_installed_metadata_matches_the_package_version() -> None:
    assert metadata.version("aha") == __version__
