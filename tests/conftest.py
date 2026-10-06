"""Shared fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def tmp_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary runtime dir containing a copy of the repo's ads.toml; $ADS_RUNTIME,
    $ADS_STATE_DIR and $ADS_PROJECT unset."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    shutil.copy(REPO / "ads.toml", runtime / "ads.toml")
    for var in ("ADS_RUNTIME", "ADS_STATE_DIR", "ADS_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    return runtime


@pytest.fixture
def tmp_state(tmp_runtime: Path, tmp_path: Path):
    """A registered project `demo` (<tmp>/demo) of tmp_runtime; returns its ProjectState."""
    from ads.projects import register
    project = tmp_path / "demo"
    project.mkdir(exist_ok=True)
    st = register(tmp_runtime, project)
    st.ensure()
    return st


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if shutil.which("tmux"):
        return
    skip = pytest.mark.skip(reason="tmux not installed")
    for item in items:
        if "tmux" in item.keywords:
            item.add_marker(skip)
