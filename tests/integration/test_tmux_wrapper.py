"""Tmux wrapper against a throwaway private server."""

from __future__ import annotations

import time
import uuid

import pytest

from ads.tmux import DEFAULT_CONF, Tmux

from tmuxhelp import kill_and_clean

pytestmark = pytest.mark.tmux


@pytest.fixture
def tmux():
    t = Tmux(f"ads-test-{uuid.uuid4().hex[:8]}", conf=DEFAULT_CONF)
    yield t
    kill_and_clean(t)


def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_conf_applied_and_paste(tmux: Tmux, tmp_path) -> None:
    pane = tmux.new_session("s", cwd=tmp_path, width=120, height=30)
    assert pane.startswith("%") and tmux.has_session("s")
    assert tmux.run("show-options", "-g", "-v", "prefix") == "C-a"
    assert tmux.run("show-options", "-s", "-v", "extended-keys") == "on"
    tmux.set_pane_opt(pane, "@ads_role", "planner")
    assert tmux.get_pane_opt(pane, "@ads_role") == "planner"
    tmux.respawn(pane, ["cat"], env={"ADS_X": "1"}, cwd=tmp_path)
    line = "[ADS-MSG id=m-20261005-000001 from=orchestrator type=instruct] Read /x.md $HOME 'q' \"d\""
    tmux.paste_line(pane, line)
    tmux.send_keys(pane, "Enter")
    assert _wait(lambda: tmux.capture(pane).count(line) >= 2)  # tty echo + cat output
    assert tmux.run("list-buffers") == ""  # -d deleted the buffer
    with pytest.raises(ValueError):
        tmux.paste_line(pane, "a\nb")
    tmux.respawn(pane, ["true"])
    assert _wait(lambda: tmux.pane_dead(pane))
    tmux.kill_session("s")
    assert not tmux.has_session("s")
