"""M5: `ads start --no-attach` / `ads stop` / existing-session handling with fake agents."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tmuxhelp import FAKE_AGENT, kill_and_clean, set_config, unique_socket, wait_for

pytestmark = pytest.mark.tmux

ADS = str(Path(sys.executable).parent / "ads")


@pytest.fixture
def env(tmp_runtime: Path, tmp_path: Path):
    sock = unique_socket()
    set_config(tmp_runtime, "ads", tmux_socket=f'"{sock}"')
    set_config(tmp_runtime, "delivery", tick_ms=100, startup_timeout_s=30)
    (tmp_runtime / "fake.json").write_text(json.dumps({"*": {"busy_s": "0.3"}}))
    e = {k: v for k, v in os.environ.items() if k not in ("TMUX", "ADS_AGENT")}
    e.update(ADS_RUNTIME=str(tmp_runtime), ADS_CLAUDE_BIN=str(FAKE_AGENT))
    from ads.tmux import Tmux
    yield e, Tmux(sock), tmp_runtime
    kill_and_clean(Tmux(sock))


def ads(e, *args, timeout=90, input=None):
    return subprocess.run([ADS, *args], env=e, capture_output=True, text=True,
                          timeout=timeout, input=input)


def test_start_status_send_stop(env, tmp_path: Path) -> None:
    e, tmux, runtime = env
    project = tmp_path / "demo"
    cp = ads(e, str(project), "--yes", "--no-attach")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "all 7 agents idle" in cp.stdout
    assert (project / ".git").is_dir() and (project / "docs").is_dir()
    assert "## Lab Notes" in (runtime / "CLAUDE.md").read_text()
    sess = json.loads((runtime / "work/run/session.json").read_text())
    assert sess["socket"] == tmux.socket and sess["project"] == str(project.resolve())
    assert tmux.has_session(sess["session"])

    st = json.loads(ads(e, "status", "--json").stdout)
    assert {a["state"] for a in st["agents"].values()} == {"idle"}
    assert st["supervisor"]["alive"]

    # the human pane runs the editor; window 0 / human pane selected
    panes = json.loads((runtime / "work/run/panes.json").read_text())
    listing = tmux.run("list-panes", "-s", "-t", f"={sess['session']}", "-F",
                       "#{window_active}#{pane_active} #{pane_id}")
    assert f"11 {panes['human']}" in listing.splitlines()

    mid = ads(e, "send", "--from", "human", "--to", "planner", "--type", "info",
              "--subject", "hi", "--body", "hello").stdout.strip()
    from ads.bus import store
    from ads.paths import Runtime
    rt = Runtime(runtime)
    assert wait_for(lambda: store.get(rt, mid).status == "delivered", timeout=15)

    # existing session, non-tty, no flag -> exit 1 with hint; --attach --no-attach -> 0
    cp = ads(e, str(project), "--no-attach")
    assert cp.returncode == 1 and "--attach or --restart" in cp.stderr
    cp = ads(e, str(project), "--attach", "--no-attach")
    assert cp.returncode == 0 and "is running" in cp.stdout

    # a second project on the same runtime is refused
    cp = ads(e, str(tmp_path / "other"), "--yes", "--no-attach")
    assert cp.returncode == 1 and "already runs a cell" in cp.stderr

    cp = ads(e, "stop")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "stopped" in cp.stdout
    assert not tmux.has_session(sess["session"])
    no_server = subprocess.run([*tmux.base(), "list-sessions"], capture_output=True)
    assert no_server.returncode != 0
    st = json.loads(ads(e, "status", "--json").stdout)
    assert not st["supervisor"]["alive"]
    assert {(a["state"], a["reason"]) for a in st["agents"].values()} == {("down", "shutdown")}


def test_restart_flag(env, tmp_path: Path) -> None:
    e, tmux, runtime = env
    project = tmp_path / "demo"
    project.mkdir()
    assert ads(e, str(project), "--no-attach").returncode == 0
    pid1 = (runtime / "work/run/supervisor.pid").read_text().strip()
    cp = ads(e, str(project), "--restart", "--no-attach")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "restarting" in cp.stdout and "all 7 agents idle" in cp.stdout
    pid2 = (runtime / "work/run/supervisor.pid").read_text().strip()
    assert pid2 and pid2 != pid1
    assert not (project / ".git").exists()  # existing project: no git init
    assert ads(e, "stop").returncode == 0
