"""M5: `ads start --no-attach` / `ads stop` / existing-session handling with fake agents, and
several projects' cells running concurrently from one runtime."""

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


class Sockets:
    """The per-project tmux servers `<prefix>-<name>` of a test runtime."""

    def __init__(self, prefix: str, runtime: Path) -> None:
        self.prefix, self.runtime = prefix, runtime

    def __call__(self, name: str):
        from ads.tmux import Tmux
        return Tmux(f"{self.prefix}-{name}")

    def cleanup(self) -> None:
        from ads.projects import all_projects
        for st in all_projects(self.runtime):
            kill_and_clean(self(st.name))


@pytest.fixture
def env(tmp_runtime: Path, tmp_path: Path):
    prefix = unique_socket()
    set_config(tmp_runtime, "ads", tmux_socket=f'"{prefix}"')
    set_config(tmp_runtime, "delivery", tick_ms=100, startup_timeout_s=30)
    (tmp_runtime / "fake.json").write_text(json.dumps({"*": {"busy_s": "0.3"}}))
    e = {k: v for k, v in os.environ.items()
         if k not in ("TMUX", "ADS_AGENT", "ADS_STATE_DIR", "ADS_PROJECT")}
    e.update(ADS_RUNTIME=str(tmp_runtime), ADS_CLAUDE_BIN=str(FAKE_AGENT))
    socks = Sockets(prefix, tmp_runtime)
    yield e, socks, tmp_runtime
    socks.cleanup()


def ads(e, *args, timeout=90, input=None, cwd=None):
    return subprocess.run([ADS, *args], env=e, capture_output=True, text=True,
                          timeout=timeout, input=input, cwd=cwd)


def test_start_status_send_stop(env, tmp_path: Path) -> None:
    e, socks, runtime = env
    tmux = socks("demo")
    state = runtime / "projects" / "demo"
    project = tmp_path / "demo"
    cp = ads(e, str(project), "--yes", "--no-attach")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "all 7 agents idle" in cp.stdout
    assert (project / ".git").is_dir() and (project / "docs").is_dir()
    claude_md = (state / "CLAUDE.md").read_text()
    assert "## Lab Notes" in claude_md and str(project) in claude_md
    assert not (runtime / "CLAUDE.md").exists() and not (runtime / "work").exists()
    assert json.loads((state / "project.json").read_text())["path"] == str(project.resolve())
    sess = json.loads((state / "work/run/session.json").read_text())
    assert sess["socket"] == tmux.socket and sess["project"] == str(project.resolve())
    assert sess["state_dir"] == str(state)
    assert tmux.has_session(sess["session"])

    st = json.loads(ads(e, "status", "--json").stdout)
    assert {a["state"] for a in st["agents"].values()} == {"idle"}
    assert st["supervisor"]["alive"]

    # the human pane runs the editor; window 0 / human pane selected
    panes = json.loads((state / "work/run/panes.json").read_text())
    listing = tmux.run("list-panes", "-s", "-t", f"={sess['session']}", "-F",
                       "#{window_active}#{pane_active} #{pane_id}")
    assert f"11 {panes['human']}" in listing.splitlines()

    mid = ads(e, "send", "--from", "human", "--to", "planner", "--type", "info",
              "--subject", "hi", "--body", "hello").stdout.strip()
    from ads.bus import store
    from ads.paths import ProjectState
    rt = ProjectState.of(runtime, "demo")
    assert wait_for(lambda: store.get(rt, mid).status == "delivered", timeout=15)
    from ads.bus.state import read_state  # let planner's turn end before `ads stop` below
    assert wait_for(lambda: read_state(rt, "planner")["state"] == "idle", timeout=15)

    # existing session, non-tty, no flag -> exit 1 with hint; --attach --no-attach -> 0
    cp = ads(e, str(project), "--no-attach")
    assert cp.returncode == 1 and "--attach or --restart" in cp.stderr
    cp = ads(e, str(project), "--attach", "--no-attach")
    assert cp.returncode == 0 and "is running" in cp.stdout

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
    e, socks, runtime = env
    project = tmp_path / "demo"
    project.mkdir()
    pidfile = runtime / "projects/demo/work/run/supervisor.pid"
    assert ads(e, str(project), "--no-attach").returncode == 0
    pid1 = pidfile.read_text().strip()
    cp = ads(e, str(project), "--restart", "--no-attach")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "restarting" in cp.stdout and "all 7 agents idle" in cp.stdout
    pid2 = pidfile.read_text().strip()
    assert pid2 and pid2 != pid1
    assert not (project / ".git").exists()  # existing project: no git init
    assert ads(e, "stop").returncode == 0


def _status(e, name: str) -> dict:
    cp = ads(e, "status", "--json", "-p", name)
    assert cp.returncode == 0, cp.stderr
    return json.loads(cp.stdout)


def test_two_projects_run_concurrently(env, tmp_path: Path) -> None:
    """Two cells from one runtime: distinct sockets and state, both idle, independent
    delivery, `ads list`, `ads stop -p A` leaves B running, `ads stop --all`."""
    e, socks, runtime = env
    pa, pb = tmp_path / "alpha", tmp_path / "beta"
    for p in (pa, pb):
        cp = ads(e, str(p), "--yes", "--no-attach")
        assert cp.returncode == 0, cp.stdout + cp.stderr
        assert "all 7 agents idle" in cp.stdout
    ta, tb = socks("alpha"), socks("beta")
    sa = json.loads((runtime / "projects/alpha/work/run/session.json").read_text())
    sb = json.loads((runtime / "projects/beta/work/run/session.json").read_text())
    assert (sa["socket"], sb["socket"]) == (ta.socket, tb.socket) and ta.socket != tb.socket
    assert ta.has_session(sa["session"]) and tb.has_session(sb["session"])
    assert not ta.has_session(sb["session"]) and not tb.has_session(sa["session"])

    for name in ("alpha", "beta"):
        st = _status(e, name)
        assert {a["state"] for a in st["agents"].values()} == {"idle"}, name
        assert st["supervisor"]["alive"] and st["project"]["name"] == name
    # without -p and with two running cells: ambiguous outside the projects ...
    cp = ads(e, "status", cwd=str(tmp_path))
    assert cp.returncode == 1 and "several projects are running" in cp.stderr
    # ... but the cwd inside a project selects it
    cp = ads(e, "status", "--json", cwd=str(pb / "docs"))
    assert json.loads(cp.stdout)["project"]["name"] == "beta"

    from ads.bus import store
    from ads.paths import ProjectState
    ra, rb = ProjectState.of(runtime, "alpha"), ProjectState.of(runtime, "beta")
    ma = ads(e, "send", "-p", "alpha", "--from", "human", "--to", "planner", "--type", "info",
             "--subject", "to alpha", "--body", "a").stdout.strip()
    mb = ads(e, "send", "-p", "beta", "--from", "human", "--to", "tester", "--type", "info",
             "--subject", "to beta", "--body", "b").stdout.strip()
    assert wait_for(lambda: store.get(ra, ma).status == "delivered", timeout=15)
    assert wait_for(lambda: store.get(rb, mb).status == "delivered", timeout=15)
    assert [m.subject for m in store.all_messages(ra)] == ["to alpha"]
    assert [m.subject for m in store.all_messages(rb)] == ["to beta"]

    cp = ads(e, "list", "--json")
    rows = {r["name"]: r for r in json.loads(cp.stdout)}
    assert set(rows) == {"alpha", "beta"}
    assert all(r["running"] for r in rows.values())
    assert rows["alpha"]["socket"] == ta.socket and rows["beta"]["socket"] == tb.socket
    assert rows["alpha"]["path"] == str(pa.resolve())
    table = ads(e, "list").stdout
    assert "alpha" in table and "beta" in table and "yes" in table

    cp = ads(e, "stop", "-p", "alpha")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert not ta.has_session(sa["session"]) and tb.has_session(sb["session"])
    st = _status(e, "beta")
    assert st["supervisor"]["alive"] and {a["state"] for a in st["agents"].values()} == {"idle"}
    rows = {r["name"]: r for r in json.loads(ads(e, "list", "--json").stdout)}
    assert rows["alpha"]["running"] is False and rows["beta"]["running"] is True
    # the single running cell is now the default project
    assert json.loads(ads(e, "status", "--json", cwd=str(tmp_path)).stdout)["project"]["name"] \
        == "beta"

    cp = ads(e, "stop", "--all")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "== beta" in cp.stdout and "== alpha" not in cp.stdout
    assert not tb.has_session(sb["session"])
    for t in (ta, tb):
        assert subprocess.run([*t.base(), "list-sessions"], capture_output=True).returncode != 0
    assert ads(e, "stop", "--all").stdout.strip() == "no running ads cells"


def test_resume_uses_per_project_session_ids(env, tmp_path: Path) -> None:
    e, socks, runtime = env
    pa, pb = tmp_path / "alpha", tmp_path / "beta"
    for p in (pa, pb):
        assert ads(e, str(p), "--yes", "--no-attach").returncode == 0
    from ads import launcher
    from ads.paths import ProjectState
    ra, rb = ProjectState.of(runtime, "alpha"), ProjectState.of(runtime, "beta")
    ids_a = {a: json.loads(launcher.session_file(ra, a).read_text())["session_id"]
             for a in ("planner", "coder-2")}
    ids_b = {a: json.loads(launcher.session_file(rb, a).read_text())["session_id"]
             for a in ("planner", "coder-2")}
    assert not set(ids_a.values()) & set(ids_b.values())
    assert ads(e, "stop", "--all").returncode == 0
    log = tmp_path / "fake.jsonl"
    (runtime / "fake.json").write_text(json.dumps({"*": {"busy_s": "0.3", "log": str(log)}}))
    cp = ads(e, str(pa), "--resume", "--no-attach")
    assert cp.returncode == 0 and "all 7 agents idle" in cp.stdout, cp.stdout + cp.stderr
    starts = {ev["agent"]: ev["argv"] for ev in map(json.loads, log.read_text().splitlines())
              if ev["event"] == "start"}
    for agent, sid in ids_a.items():
        argv = starts[agent]
        assert argv[argv.index("--resume") + 1] == sid
        assert argv[argv.index("--add-dir") + 1] == str(ra.dir)
    assert ads(e, "stop", "-p", "alpha").returncode == 0
