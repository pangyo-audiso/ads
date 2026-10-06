"""M3b: supervisor delivery loop against fake agents on a private tmux server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ads import dialogs
from ads.bus import store

from tmuxhelp import REPO

pytestmark = pytest.mark.tmux


def test_idle_delivery_confirmed_via_hook(make_cell) -> None:
    cell = make_cell()
    assert cell.all_idle()
    m = cell.send(from_="human", to="planner", type="info", subject="hello")
    assert cell.run_until(lambda: cell.msg(m.id).status == "delivered")
    msg = cell.msg(m.id)
    assert msg.enters == 0 and msg.pastes == 0 and not msg.unconfirmed
    (submit,) = cell.events("planner", "submit")
    assert submit["text"].startswith(f"[ADS-MSG id={m.id} from=human type=info] Read ")
    assert submit["text"].endswith(f"{m.id}.md and follow the ADS protocol.")
    assert cell.run_until(lambda: cell.state("planner")["state"] == "idle")
    assert cell.state("planner")["inflight_msg"] is None
    # nobody else got anything
    assert [e["agent"] for e in cell.events(event="submit")] == ["planner"]
    # border mirrored
    assert cell.tmux.display(cell.panes["planner"], "#{@ads_state}") == "idle"
    log = (cell.rt.logs / "supervisor.log").read_text()
    assert f"{m.id} confirmed by hook" in log


def test_queued_while_busy_delivered_in_seq_order(make_cell) -> None:
    cell = make_cell(fake={"tester": {"busy_s": "2"}})
    assert cell.all_idle()
    first = cell.send(from_="human", to="tester", type="info", subject="first")
    assert cell.run_until(lambda: cell.state("tester")["state"] == "busy")
    rest = [cell.send(from_="orchestrator", to="tester", type="info", subject=f"n{i}")
            for i in range(3)]
    for _ in range(5):  # still busy: nothing else is pasted
        cell.sup.run_once()
    assert [cell.msg(m.id).status for m in rest] == ["queued"] * 3
    assert len(cell.events("tester", "submit")) == 1
    ids = [first.id] + [m.id for m in rest]
    assert cell.run_until(lambda: all(cell.msg(i).status == "delivered" for i in ids), timeout=30)
    submitted = [e["text"].split()[1].removeprefix("id=") for e in cell.events("tester", "submit")]
    assert submitted == ids
    assert [cell.msg(i).seq for i in ids] == sorted(cell.msg(i).seq for i in ids)
    assert not cell.events("tester", "enter-ignored-busy")  # never pasted while busy


def test_enter_retry_when_first_enter_swallowed(make_cell) -> None:
    cell = make_cell(fake={"developer": {"swallow_enter": "1"}})
    assert cell.all_idle()
    m = cell.send(from_="human", to="developer", type="info")
    assert cell.run_until(lambda: cell.msg(m.id).status == "delivered", timeout=20)
    msg = cell.msg(m.id)
    assert msg.enters == 1 and not msg.unconfirmed
    assert len(cell.events("developer", "enter-swallowed")) == 1
    assert len(cell.events("developer", "submit")) == 1


def test_unconfirmed_enter_exhausted_fails(make_cell) -> None:
    cell = make_cell(fake={"coder-1": {"swallow_enter": "99"}})
    assert cell.all_idle()
    m = cell.send(from_="human", to="coder-1", type="info")
    assert cell.run_until(lambda: cell.msg(m.id).status == "failed", timeout=30)
    msg = cell.msg(m.id)
    assert msg.enters == 2  # max_enter_retries
    assert len(cell.events("coder-1", "enter-swallowed")) == 3  # first Enter + 2 retries
    alerts = [json.loads(p.read_text()) for p in cell.rt.alerts.glob("*.json")]
    assert any(a["error_type"] == "delivery_failed" and m.id in a["error_message"]
               and "--requeue" in a["error_message"] for a in alerts)
    # the stale pointer was cleared from the input box (C-c) and nothing more is pasted
    assert cell.run_until(lambda: dialogs.input_box_text(cell.capture("coder-1")) == "", timeout=5)
    for _ in range(10):
        cell.sup.run_once()
    assert cell.msg(m.id).status == "failed" and cell.state("coder-1")["inflight_msg"] is None
    # `ads send --requeue` path: failed → queued is legal and gets pasted again
    assert store.update(cell.rt, m.id, status="queued", enters=0).status == "queued"
    assert cell.run_until(lambda: len(cell.events("coder-1", "enter-swallowed")) == 4)


def test_busy_without_hook_is_delivered_unconfirmed(make_cell) -> None:
    cell = make_cell(fake={"coder-2": {"skip_hooks": "prompt-submit", "busy_s": "6"}})
    assert cell.all_idle()
    m = cell.send(from_="human", to="coder-2", type="info")
    assert cell.run_until(lambda: cell.msg(m.id).status != "queued")
    assert cell.run_until(lambda: cell.msg(m.id).status == "delivered", timeout=10)
    msg = cell.msg(m.id)
    assert msg.unconfirmed and msg.enters == 0
    assert "UNCONFIRMED" in (cell.rt.logs / "supervisor.log").read_text()


def test_second_supervisor_exits_1(make_cell) -> None:
    cell = make_cell()
    env = {**os.environ, "ADS_RUNTIME": str(cell.rt.runtime), "ADS_STATE_DIR": str(cell.rt.dir)}
    cp = subprocess.run([str(Path(sys.executable).parent / "ads"), "supervisor", "--runtime",
                         str(cell.rt.runtime)], capture_output=True, text=True, env=env, timeout=30,
                        cwd=REPO)
    assert cp.returncode == 1, cp.stderr
    assert "already running" in cp.stderr and str(os.getpid()) in cp.stderr
    # the holder's pid file is intact
    assert cell.rt.supervisor_pid.read_text().strip() == str(os.getpid())
