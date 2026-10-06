"""M3a: ads layout on a private tmux server + the fake claude agent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ads import dialogs, launcher
from ads.config import load_config
from ads.paths import AGENTS, LAYOUT, ProjectState
from ads.tmux import DEFAULT_CONF, Tmux, build_layout, read_panes

from tmuxhelp import FAKE_AGENT, fake_log, wait_for

pytestmark = pytest.mark.tmux

POINTER = ("[ADS-MSG id=m-20261005-000001 from=orchestrator type=info] "
           "Read /tmp/x/work/msgs/m-20261005-000001.md and follow the ADS protocol.")


@pytest.fixture
def cell(tmux: Tmux, tmp_runtime: Path, tmp_path: Path):
    from ads.projects import register
    project = tmp_path / "project"
    project.mkdir()
    rt = register(tmp_runtime, project)
    rt.ensure()
    panes = build_layout(tmux, "ads-test", project, rt)
    assert rt.panes_json.is_file() and rt.panes_json.is_relative_to(tmp_runtime / "projects")
    return tmux, rt, project, panes


def _fake_env(rt: ProjectState, project: Path, agent: str, log: Path, **knobs: str) -> dict[str, str]:
    cfg = load_config(runtime=rt.runtime)
    env = launcher.agent_env(cfg, rt, project, agent)
    env["FAKE_LOG"] = str(log)
    env.update({f"FAKE_{k.upper()}": v for k, v in knobs.items()})
    return env


def test_layout_windows_roles_and_panes_json(cell) -> None:
    tmux, rt, _project, panes = cell
    listing = tmux.run("list-panes", "-s", "-t", "=ads-test",
                       "-F", "#{window_index} #{window_name} #{pane_index} #{pane_id} #{@ads_role}")
    rows = [line.split() for line in listing.splitlines()]
    by_window: dict[str, list[list[str]]] = {}
    for r in rows:
        by_window.setdefault(r[0], []).append(r)
    assert sorted(by_window) == ["0", "1", "2"]
    assert len(by_window["0"]) == 4 and len(by_window["1"]) == 4 and len(by_window["2"]) == 1
    assert {r[1] for r in by_window["0"]} == {"agents"}
    assert {r[1] for r in by_window["1"]} == {"team"}
    assert by_window["2"][0][1] == "supervisor"
    for win, name, idx, pane_id, role in rows:
        if role == "supervisor":
            assert win == "2"
            continue
        assert LAYOUT[role] == (int(win), int(idx))
        assert panes[role] == pane_id
    # panes.json
    data = json.loads(rt.panes_json.read_text())
    assert data == read_panes(rt) == panes
    assert set(data) == {*AGENTS, "human", "supervisor"}
    assert all(v.startswith("%") for v in data.values())
    # size and placeholder
    assert tmux.display("=ads-test:0", "#{window_width}x#{window_height}") == "240x70"
    assert tmux.display(panes["planner"], "#{pane_current_command}") == "sleep"
    assert tmux.display(panes["planner"], "#{@ads_state}") == "-"
    # active window/pane is the human editor
    assert tmux.display("=ads-test:", "#{window_index}") == "0"


def test_conf_loaded(cell) -> None:
    tmux, *_ = cell
    assert tmux.conf == DEFAULT_CONF  # tmp runtime has no src/ads/tmux -> packaged copy
    assert tmux.run("show-options", "-s", "-v", "extended-keys") == "on"
    assert tmux.run("show-options", "-g", "-v", "prefix") == "C-a"
    assert tmux.run("show-options", "-g", "-v", "remain-on-exit") == "on"


def test_layout_uses_runtime_conf(tmux: Tmux, tmp_path: Path) -> None:
    rt_root = tmp_path / "rt"
    conf_dir = rt_root / "src" / "ads" / "tmux"
    conf_dir.mkdir(parents=True)
    (conf_dir / "ads.tmux.conf").write_text(DEFAULT_CONF.read_text() + "\nset -g @ads_marker yes\n")
    project = tmp_path / "p"
    project.mkdir()
    build_layout(tmux, "s2", project, ProjectState.of(rt_root, "p"))
    assert tmux.conf == conf_dir / "ads.tmux.conf"
    assert tmux.run("show-options", "-g", "-v", "@ads_marker") == "yes"


def test_fake_agent_paste_submit_hooks_and_busy(cell, tmp_path: Path) -> None:
    tmux, rt, project, panes = cell
    pane = panes["planner"]
    log = tmp_path / "fake.jsonl"
    env = _fake_env(rt, project, "planner", log, busy_s="1.5")
    tmux.respawn(pane, [str(FAKE_AGENT), "--model", "x", "--session-id", "abc"], env, project)
    assert wait_for(lambda: dialogs.has_input_box(tmux.capture(pane)))
    assert wait_for(lambda: any(e["event"] == "hook" and e["hook"] == "session-start"
                                for e in fake_log(log)))
    screen = tmux.capture(pane)
    assert "ads-planner" in screen and not dialogs.is_busy(screen)
    assert dialogs.match_dialog(screen) is None and not dialogs.is_unknown_dialog(screen)

    tmux.paste_line(pane, POINTER)
    assert wait_for(lambda: "m-20261005-000001" in (dialogs.input_box_text(tmux.capture(pane)) or ""))
    tmux.send_keys(pane, "Enter")
    assert wait_for(lambda: dialogs.is_busy(tmux.capture(pane)), timeout=3)
    assert dialogs.input_box_text(tmux.capture(pane)) == ""
    assert "❯ " + POINTER[:40] in tmux.capture(pane)  # transcript echo, ASCII space
    assert wait_for(lambda: not dialogs.is_busy(tmux.capture(pane)), timeout=5)
    hooks = [e["hook"] for e in fake_log(log) if e["event"] == "hook"]
    assert hooks == ["session-start", "prompt-submit", "stop"]
    submitted = [e for e in fake_log(log) if e["event"] == "submit"]
    assert submitted[0]["text"] == POINTER
    # the real hook handler ran against the runtime
    events = [json.loads(line) for line in (rt.logs / "hooks.log").read_text().splitlines()]
    assert [e["event"] for e in events if "payload" in e] == ["session-start", "prompt-submit", "stop"]
    assert events[1]["payload"]["prompt"] == POINTER


def test_fake_agent_ignores_enter_while_busy(cell, tmp_path: Path) -> None:
    tmux, rt, project, panes = cell
    pane = panes["tester"]
    log = tmp_path / "fake.jsonl"
    tmux.respawn(pane, [str(FAKE_AGENT)], _fake_env(rt, project, "tester", log, busy_s="2"), project)
    assert wait_for(lambda: dialogs.has_input_box(tmux.capture(pane)))
    tmux.send_keys(pane, "hello", literal=True)
    tmux.send_keys(pane, "Enter")
    assert wait_for(lambda: dialogs.is_busy(tmux.capture(pane)), timeout=3)
    tmux.paste_line(pane, "second")
    tmux.send_keys(pane, "Enter")
    assert wait_for(lambda: any(e["event"] == "enter-ignored-busy" for e in fake_log(log)), timeout=3)
    assert dialogs.input_box_text(tmux.capture(pane)) == "second"


@pytest.mark.parametrize("which", ["trust", "bypass"])
def test_fake_agent_dialog(cell, tmp_path: Path, which: str) -> None:
    tmux, rt, project, panes = cell
    pane = panes["evaluator"]
    log = tmp_path / "fake.jsonl"
    tmux.respawn(pane, [str(FAKE_AGENT)], _fake_env(rt, project, "evaluator", log, dialog=which),
                 project)
    assert wait_for(lambda: dialogs.match_dialog(tmux.capture(pane)))
    d = dialogs.match_dialog(tmux.capture(pane))
    assert d.name == which and not dialogs.accept_selected(d, tmux.capture(pane))
    tmux.send_keys(pane, "Down")
    assert wait_for(lambda: dialogs.accept_selected(d, tmux.capture(pane)))
    tmux.send_keys(pane, "Enter")
    assert wait_for(lambda: dialogs.has_input_box(tmux.capture(pane)))
    assert wait_for(lambda: any(e.get("hook") == "session-start" for e in fake_log(log)))


def test_fake_agent_dialog_no_exit_and_pane_dead(cell, tmp_path: Path) -> None:
    tmux, rt, project, panes = cell
    pane = panes["developer"]
    log = tmp_path / "fake.jsonl"
    tmux.respawn(pane, [str(FAKE_AGENT)], _fake_env(rt, project, "developer", log, dialog="trust"),
                 project)
    assert wait_for(lambda: dialogs.match_dialog(tmux.capture(pane)))
    assert not tmux.pane_dead(pane)
    tmux.send_keys(pane, "Enter")  # default is "No, exit"
    assert wait_for(lambda: tmux.pane_dead(pane), timeout=5)


def test_fake_agent_exit_after_gives_dead_pane(cell, tmp_path: Path) -> None:
    tmux, rt, project, panes = cell
    pane = panes["coder-1"]
    log = tmp_path / "fake.jsonl"
    tmux.respawn(pane, [str(FAKE_AGENT)],
                 _fake_env(rt, project, "coder-1", log, exit_after="1", busy_s="0.3"), project)
    assert wait_for(lambda: dialogs.has_input_box(tmux.capture(pane)))
    tmux.paste_line(pane, "go")
    tmux.send_keys(pane, "Enter")
    assert wait_for(lambda: tmux.pane_dead(pane), timeout=5)
    assert [e["hook"] for e in fake_log(log) if e["event"] == "hook"] == ["session-start",
                                                                          "prompt-submit"]
