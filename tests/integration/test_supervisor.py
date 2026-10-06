"""M3c: dialog watchdog (gated), down/restart, hold release, supersede, SIGTERM shutdown."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ads import cli, dialogs, launcher
from ads.bus import ledger, store
from ads.paths import AGENTS
from ads.supervisor import write_request

from tmuxhelp import FAKE_AGENT, FIXTURES, REPO, wait_for

pytestmark = pytest.mark.tmux


def _keys(cell, agent: str) -> list[str]:
    return [e["key"] for e in cell.events(agent, "key")]


def _sup_log(cell) -> str:
    return (cell.rt.logs / "supervisor.log").read_text()


def test_startup_dialogs_answered_while_starting(make_cell) -> None:
    cell = make_cell(fake={"planner": {"dialog": "trust,bypass"}, "evaluator": {"dialog": "trust"},
                           "coder-2": {"dialog": "bypass"}})
    assert cell.all_idle()
    accepted = [(e["agent"], e["dialog"]) for e in cell.events(event="dialog-accepted")]
    assert sorted(accepted) == sorted([("planner", "trust"), ("planner", "bypass"),
                                       ("evaluator", "trust"), ("coder-2", "bypass")])
    assert _keys(cell, "planner") == ["down", "enter", "down", "enter"]
    assert not cell.events("planner", "exit")
    log = _sup_log(cell)
    assert "planner: trust dialog answered" in log and "planner: bypass dialog answered" in log
    # agents without dialogs never got a key
    assert _keys(cell, "tester") == []


def test_negative_fixture_on_screen_is_not_answered(make_cell, tmp_path: Path) -> None:
    # A resumed session re-renders old output containing dialog text. While `starting` the
    # watchdog runs but must not match (anchored tail + input box); once idle it is gated off.
    lines = (FIXTURES / "agent_output_dialog_text.txt").read_text(encoding="utf-8").splitlines()
    cut = next(i for i, ln in enumerate(lines) if ln.startswith("✻ Churned"))
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("\n".join(lines[: cut + 1]) + "\n", encoding="utf-8")
    cell = make_cell(fake={"tester": {"transcript": str(transcript), "session_start_delay": "3"}})
    t0 = time.time()
    assert cell.run_until(lambda: "Yes, I trust this folder" in cell.capture("tester"))
    screen = cell.capture("tester")
    assert dialogs.has_input_box(screen) and dialogs.match_dialog(screen) is None
    assert cell.state("tester")["state"] == "starting"
    assert cell.all_idle()
    assert time.time() - t0 >= 2.5  # it really sat in `starting` with the text on screen
    for _ in range(20):
        cell.sup.run_once()
    assert _keys(cell, "tester") == []
    assert "tester: " not in "\n".join(ln for ln in _sup_log(cell).splitlines() if "dialog" in ln)


def test_dialog_on_idle_agent_is_gated_off(make_cell) -> None:
    # session-start fires first (→ idle), then a real trust dialog is drawn: not answered.
    cell = make_cell(fake={"developer": {"dialog": "trust", "session_start_first": "1"}})
    assert cell.all_idle()
    assert cell.run_until(lambda: dialogs.match_dialog(cell.capture("developer")))
    for _ in range(20):
        cell.sup.run_once()
    assert _keys(cell, "developer") == []
    assert cell.state("developer")["state"] == "idle"


def test_unknown_dialog_sets_state_dialog_then_cleared(make_cell, tmp_path: Path) -> None:
    unknown = tmp_path / "unknown.txt"
    unknown.write_text(" Something new happened:\n\n ❯ Continue\n   Stop\n\n"
                       " Enter to confirm · Esc to cancel\n", encoding="utf-8")
    cell = make_cell(fake={"coder-1": {"dialog": str(unknown), "session_start_delay": "2"}})
    assert cell.run_until(lambda: cell.state("coder-1")["state"] == "dialog")
    assert cell.state("coder-1")["reason"] == "unknown"
    assert cell.tmux.display(cell.panes["coder-1"], "#{@ads_state}") == "dialog:unknown"
    for _ in range(10):
        cell.sup.run_once()
    assert _keys(cell, "coder-1") == []  # surfaced, never guessed
    assert "unknown dialog" in _sup_log(cell)
    # a human dismisses it → input box → dialog_cleared → starting → SessionStart → idle
    cell.tmux.send_keys(cell.panes["coder-1"], "Down")
    cell.tmux.send_keys(cell.panes["coder-1"], "Enter")
    assert cell.run_until(lambda: cell.state("coder-1")["state"] == "starting", timeout=5)
    assert cell.state("coder-1")["reason"] == "dialog-cleared"
    assert cell.run_until(lambda: cell.state("coder-1")["state"] == "idle")


def test_held_message_released_after_blocking_task_closes(make_cell) -> None:
    cell = make_cell(fake={"planner": {"reply": "1", "busy_s": "1.5"},
                           "developer": {"reply": "1"}})
    assert cell.all_idle()
    t1 = cell.send(from_="orchestrator", to="planner", type="instruct", subject="plan it")
    t2 = cell.send(from_="orchestrator", to="developer", type="instruct", subject="build it")
    assert t1.status == "queued" and t2.status == "held" and t2.held_by == t1.id
    assert cell.run_until(lambda: cell.msg(t1.id).status == "delivered")
    for _ in range(3):
        cell.sup.run_once()
    assert cell.msg(t2.id).status == "held"  # still held while planner works
    assert cell.run_until(lambda: ledger.get_task(cell.rt, t1.id)["state"] == "closed", timeout=20)
    assert cell.run_until(lambda: cell.msg(t2.id).status == "delivered", timeout=20)
    assert cell.run_until(lambda: ledger.get_task(cell.rt, t2.id)["state"] == "closed", timeout=20)
    # both reports reached the orchestrator pane
    reports = [m for m in store.all_messages(cell.rt) if m.type == "report"]
    assert {m.re for m in reports} == {t1.id, t2.id}
    assert cell.run_until(lambda: all(cell.msg(m.id).status == "delivered" for m in reports))
    orch = [e["text"] for e in cell.events("orchestrator", "submit")]
    assert [r.id in " ".join(orch) for r in reports] == [True, True]


def test_supersede_cancels_child(make_cell) -> None:
    cell = make_cell(fake={"evaluator": {"busy_s": "8"}})
    assert cell.all_idle()
    busy = cell.send(from_="human", to="evaluator", type="info", subject="keep busy")
    assert cell.run_until(lambda: cell.msg(busy.id).status == "delivered")
    t1 = cell.send(from_="orchestrator", to="planner", type="instruct", subject="plan v1")
    assert cell.run_until(lambda: cell.msg(t1.id).status == "delivered")
    child = cell.send(from_="planner", to="evaluator", type="review-request", subject="review",
                      parent=t1.id)
    for _ in range(3):
        cell.sup.run_once()
    assert cell.msg(child.id).status == "queued"  # evaluator busy
    new = cell.send(from_="orchestrator", to="planner", type="instruct", subject="plan v2",
                    supersede=t1.id)
    assert new.status == "queued"  # supersede bypasses the hold
    assert ledger.get_task(cell.rt, t1.id)["state"] == "superseded"
    assert ledger.get_task(cell.rt, child.id)["state"] == "superseded"
    assert cell.msg(child.id).status == "superseded"
    assert cell.run_until(lambda: cell.msg(new.id).status == "delivered", timeout=20)
    texts = [e["text"] for e in cell.events("planner", "submit")]
    assert texts[-1].endswith(f"SUPERSEDES {t1.id}: abort that task first.")
    # the superseded child is never pasted to the evaluator
    assert cell.run_until(lambda: cell.state("evaluator")["state"] == "idle", timeout=20)
    for _ in range(10):
        cell.sup.run_once()
    assert not any(child.id in e["text"] for e in cell.events("evaluator", "submit"))


def test_exit_down_after_grace_cascade_then_restart(make_cell) -> None:
    cell = make_cell(fake={"planner": {"exit_after": "1"}})
    assert cell.all_idle()
    t1 = cell.send(from_="orchestrator", to="planner", type="instruct", subject="doomed")
    assert cell.run_until(lambda: cell.tmux.pane_dead(cell.panes["planner"]), timeout=10)
    dead_at = time.time()
    assert cell.state("planner")["state"] == "busy"  # crash: no Stop/SessionEnd hook
    assert cell.run_until(lambda: cell.state("planner")["state"] == "down", timeout=10)
    assert time.time() - dead_at >= 0.9  # dead_grace_s = 1
    assert cell.state("planner")["reason"] == "pane_dead"
    assert ledger.get_task(cell.rt, t1.id)["state"] == "failed"
    (sysmsg,) = [m for m in store.all_messages(cell.rt) if m.type == "system"]
    assert (sysmsg.from_, sysmsg.to, sysmsg.result, sysmsg.re) == \
        ("ads", "orchestrator", "agent-down", t1.id)
    assert cell.run_until(lambda: cell.msg(sysmsg.id).status == "delivered")
    assert any(sysmsg.id in e["text"] for e in cell.events("orchestrator", "submit"))
    assert cell.tmux.display(cell.panes["planner"], "#{@ads_state}") == "down:pane_dead"
    for _ in range(10):  # cascade happens once
        cell.sup.run_once()
    assert len([m for m in store.all_messages(cell.rt) if m.type == "system"]) == 1

    # restart --resume via the CLI request → respawned with the same session uuid → idle
    cell.fake("planner", exit_after="0")
    uuid = json.loads(launcher.session_file(cell.rt, "planner").read_text())["session_id"]
    assert cli.main(["restart", "planner", "--resume", "--runtime", str(cell.rt.root)]) == 0
    assert cell.run_until(lambda: cell.state("planner")["state"] == "idle", timeout=15)
    starts = cell.events("planner", "start")
    assert len(starts) == 2
    argv = starts[-1]["argv"]
    assert argv[argv.index("--resume") + 1] == uuid and "--session-id" not in argv
    assert cell.state("planner")["reason"] == "resume"
    assert not cell.tmux.pane_dead(cell.panes["planner"])
    # and it works again
    m = cell.send(from_="human", to="planner", type="info", subject="welcome back")
    assert cell.run_until(lambda: cell.msg(m.id).status == "delivered")


def test_restart_without_resume_gets_fresh_session(make_cell) -> None:
    cell = make_cell()
    assert cell.all_idle()
    old = json.loads(launcher.session_file(cell.rt, "tester").read_text())["session_id"]
    write_request(cell.rt, "restart", agent="tester", resume=False)
    assert cell.run_until(lambda: len(cell.events("tester", "start")) == 2)
    assert cell.run_until(lambda: cell.state("tester")["state"] == "idle")
    argv = cell.events("tester", "start")[-1]["argv"]
    new = argv[argv.index("--session-id") + 1]
    assert new != old and "--resume" not in argv
    assert not list(cell.rt.requests.glob("*.json"))  # consumed


def test_sigterm_shuts_down_without_cascade(make_cell) -> None:
    cell = make_cell(fake={"planner": {"busy_s": "60"}}, start=False)
    env = {**os.environ, "ADS_RUNTIME": str(cell.rt.root), "ADS_CLAUDE_BIN": str(FAKE_AGENT)}
    proc = subprocess.Popen([str(Path(sys.executable).parent / "ads"), "supervisor", "--runtime",
                             str(cell.rt.root)], env=env, cwd=REPO, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        assert wait_for(lambda: set(cell.states().values()) == {"idle"}, timeout=20)
        assert cell.rt.supervisor_pid.read_text().strip() == str(proc.pid)
        t1 = cell.send(from_="orchestrator", to="planner", type="instruct", subject="long job")
        assert wait_for(lambda: cell.msg(t1.id).status == "delivered", timeout=10)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, out
    states = {a: cell.state(a) for a in AGENTS}
    assert {(s["state"], s["reason"]) for s in states.values()} == {("down", "shutdown")}
    assert ledger.get_task(cell.rt, t1.id)["state"] == "delivered"  # no cascade
    assert not [m for m in store.all_messages(cell.rt) if m.type == "system"]
    assert "shutdown" in out
    assert cell.rt.supervisor_pid.read_text().strip() == ""  # lock released
