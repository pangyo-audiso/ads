"""Fixtures for tmux integration tests."""

from __future__ import annotations

import pytest

from ads.tmux import Tmux

from tmuxhelp import kill_and_clean, unique_socket


@pytest.fixture
def tmux():
    t = Tmux(unique_socket())
    yield t
    kill_and_clean(t)


@pytest.fixture
def make_cell(tmux: Tmux, tmp_runtime, tmp_path, monkeypatch):
    """Factory: build a full ads cell on a private server with fake agents; returns a Cell.

    Usage: cell = make_cell(fake={"*": {...}, "planner": {...}}, delivery={...}) — the
    supervisor is constructed (lock acquired) and started (all agents launched).
    """
    from tmuxhelp import Cell
    cells = []

    def make(fake=None, delivery=None, protocol=None, start=True, project_name="project"):
        cell = Cell.create(tmux, tmp_runtime, tmp_path / project_name, monkeypatch,
                           fake=fake, delivery=delivery, protocol=protocol)
        cells.append(cell)
        if start:
            cell.start()
        return cell

    yield make
    for c in cells:
        c.close()
