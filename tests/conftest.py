"""Shared fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def tmp_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary runtime dir containing a copy of the repo's ads.toml; $ADS_RUNTIME unset."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    shutil.copy(REPO / "ads.toml", runtime / "ads.toml")
    monkeypatch.delenv("ADS_RUNTIME", raising=False)
    return runtime


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if shutil.which("tmux"):
        return
    skip = pytest.mark.skip(reason="tmux not installed")
    for item in items:
        if "tmux" in item.keywords:
            item.add_marker(skip)
