"""Pane-3 editor: submit logic and prompt_toolkit key wiring (plan §9, M4)."""

from __future__ import annotations

import json
import os
import termios
import threading
import time
from pathlib import Path

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ads import editor as E
from ads.bus import ledger as L
from ads.bus import store
from ads.config import default_config
from ads.paths import ProjectState

EXIT = "\x03\x03"  # C-c C-c


@pytest.fixture
def rt(tmp_state: ProjectState) -> ProjectState:
    return tmp_state


@pytest.fixture
def cfg():
    return default_config()


def sent(rt: ProjectState) -> list:
    return [m for m in store.all_messages(rt) if m.from_ == "human"]


def run_keys(rt: ProjectState, cfg, *chunks: str, gap: float = 0.15) -> E.Editor:
    """Feed key chunks into a live editor (`gap` seconds apart), then C-c C-c."""
    with create_pipe_input() as inp:
        ed = E.Editor(rt, cfg, input=inp, output=DummyOutput(), tick_s=0.05)
        seq = [*chunks[:-1], chunks[-1] + EXIT] if chunks else [EXIT]

        def feed() -> None:
            for i, c in enumerate(seq):
                if i:
                    time.sleep(gap)
                inp.send_text(c)
        t = threading.Thread(target=feed)
        t.start()
        assert ed.run() == 0
        t.join()
    return ed


def ask(rt: ProjectState, cfg, subject: str = "Which DB?") -> str:
    return L.send(rt, cfg, from_="orchestrator", to="human", type="question",
                  subject=subject, body="Postgres or SQLite?\n").id


# --- submit_text (no app) ---------------------------------------------------------------

def test_submit_instruct(rt, cfg):
    out = E.submit_text(rt, cfg, "Build a todo app\nwith tests\n")
    [m] = sent(rt)
    assert (m.to, m.type, m.subject) == ("orchestrator", "instruct", "Build a todo app")
    assert store.read_body(rt, m.id) == "Build a todo app\nwith tests\n"
    assert m.id in out and "instruct" in out


def test_subject_truncated(rt, cfg):
    E.submit_text(rt, cfg, "\n  " + "x" * 200)
    [m] = sent(rt)
    assert len(m.subject) <= 80


def test_blank_does_nothing(rt, cfg):
    assert E.submit_text(rt, cfg, "  \n \n") == ""
    assert sent(rt) == []


def test_auto_answer_open_question(rt, cfg):
    qid = ask(rt, cfg)
    out = E.submit_text(rt, cfg, "SQLite please")
    [m] = sent(rt)
    assert (m.type, m.re, m.to) == ("answer", qid, "orchestrator")
    assert "answer" in out
    assert L.get_task(rt, qid)["state"] == "closed"
    # question closed -> next input is an instruct again
    E.submit_text(rt, cfg, "next thing")
    assert sent(rt)[-1].type == "instruct"


def test_bang_forces_instruct(rt, cfg):
    qid = ask(rt, cfg)
    E.submit_text(rt, cfg, "!do something else")
    [m] = sent(rt)
    assert m.type == "instruct" and m.subject == "do something else"
    assert store.read_body(rt, m.id) == "do something else\n"
    assert L.get_task(rt, qid)["state"] in L.OPEN


def test_ads_instruct_forces_instruct(rt, cfg):
    ask(rt, cfg)
    E.submit_text(rt, cfg, "/ads instruct line one\nline two")
    [m] = sent(rt)
    assert m.type == "instruct"
    assert store.read_body(rt, m.id) == "line one\nline two\n"


def test_restart_request(rt, cfg):
    out = E.submit_text(rt, cfg, "/ads restart coder-1")
    [req] = list(rt.requests.glob("*-restart.json"))
    data = json.loads(req.read_text())
    assert data["op"] == "restart" and data["agent"] == "coder-1" and data["resume"] is False
    assert rt.poke.exists()
    assert "coder-1" in out
    E.submit_text(rt, cfg, "/ads restart supervisor --resume")
    assert len(list(rt.requests.glob("*-restart.json"))) == 2
    assert "usage" in E.submit_text(rt, cfg, "/ads restart nobody")
    assert len(list(rt.requests.glob("*-restart.json"))) == 2
    assert sent(rt) == []


def test_commands_render(rt, cfg):
    qid = ask(rt, cfg)
    status = E.submit_text(rt, cfg, "/ads status")
    assert "orchestrator" in status and "phase" in status
    assert status.startswith(f"project: demo ({rt.runtime.parent / 'demo'})")
    inbox = E.submit_text(rt, cfg, "/ads inbox")
    assert qid in inbox and "OPEN QUESTION" in inbox
    assert "/ads restart" in E.submit_text(rt, cfg, "/ads help")
    assert "unknown command" in E.submit_text(rt, cfg, "/ads frobnicate")
    assert sent(rt) == []


def test_toolbar(rt, cfg):
    lines = E.toolbar_lines(rt)
    assert "orch:down" in lines[0] and "phase:-" in lines[0]
    qid = ask(rt, cfg)
    lines = E.toolbar_lines(rt)
    assert f"answering Q {qid}" in lines[1]
    assert "question from orchestrator" in lines[1]


# --- app wiring -------------------------------------------------------------------------

def test_app_enter_submits(rt, cfg):
    run_keys(rt, cfg, "hello world\r")
    [m] = sent(rt)
    assert (m.type, m.subject, m.to) == ("instruct", "hello world", "orchestrator")


def test_app_newlines_do_not_submit(rt, cfg):
    run_keys(rt, cfg, "line1\nline2\x1b\rline3")  # C-j and Alt+Enter, no Enter
    assert sent(rt) == []


def test_app_multiline_body(rt, cfg):
    run_keys(rt, cfg, "line1\nline2\x1b\rline3\r")
    [m] = sent(rt)
    assert store.read_body(rt, m.id) == "line1\nline2\nline3\n"
    assert m.subject == "line1"


def test_app_empty_enter_does_nothing(rt, cfg):
    run_keys(rt, cfg, "\r  \r")
    assert sent(rt) == []


def test_app_auto_answer(rt, cfg):
    qid = ask(rt, cfg)
    run_keys(rt, cfg, "use sqlite\r")
    [m] = sent(rt)
    assert m.type == "answer" and m.re == qid


def test_app_bang(rt, cfg):
    ask(rt, cfg)
    run_keys(rt, cfg, "!new task\r")
    [m] = sent(rt)
    assert m.type == "instruct" and m.subject == "new task"


def test_app_history(rt, cfg):
    # Enter; then (after async history load) Up recalls it and Enter resends it
    run_keys(rt, cfg, "first input\r", "\x1b[A", "\r")
    msgs = sent(rt)
    assert [m.subject for m in msgs] == ["first input", "first input"]
    hist = (rt.dir / cfg.editor.history_file).read_text()
    assert rt.dir / cfg.editor.history_file == rt.input_history
    assert "+first input" in hist


def test_app_cc_clears(rt, cfg):
    ed = None
    with create_pipe_input() as inp:
        ed = E.Editor(rt, cfg, input=inp, output=DummyOutput())
        inp.send_text("junk\x03")  # single C-c clears, does not exit
        # then type and submit, then exit
        inp.send_text("kept\r" + EXIT)
        assert ed.run() == 0
    assert [m.subject for m in sent(rt)] == ["kept"]


def test_app_restart_command(rt, cfg):
    ed = run_keys(rt, cfg, "/ads restart coder-1\r")
    assert len(list(rt.requests.glob("*-restart.json"))) == 1
    assert any("restart requested: coder-1" in p for p in ed.printed)
    assert sent(rt) == []


def test_incoming_message_printed(rt, cfg):
    with create_pipe_input() as inp:
        ed = E.Editor(rt, cfg, input=inp, output=DummyOutput(), tick_s=0.01)
        ask(rt, cfg, "Pick one")
        new = ed._refresh()
        assert [m.subject for m in new] == ["Pick one"]
        assert "QUESTION" in E.render_incoming(rt, new[0])
        assert ed._refresh() == []
        assert ed._prompt_message() == "answer> "


# --- key probe --------------------------------------------------------------------------

def test_key_probe_restores_tty():
    master, slave = os.openpty()
    try:
        before = termios.tcgetattr(slave)
        threading.Timer(0.05, os.write, (master, b"\x1b[13;2u")).start()
        out: list[str] = []
        assert E.key_probe(0.2, fd=slave, out=out.append) == 0
        assert termios.tcgetattr(slave) == before
        assert repr(b"\x1b[13;2u") in out
    finally:
        os.close(master)
        os.close(slave)


def test_key_probe_not_tty(tmp_path):
    with open(tmp_path / "f", "w+") as f:
        out: list[str] = []
        assert E.key_probe(0.1, fd=f.fileno(), out=out.append) == 1


def test_app_vi_navigation_enter_submits(rt, cfg):
    assert cfg.editor.vi_mode
    # Esc -> navigation mode, pause > ESC_TIMEOUT_S, then Enter
    run_keys(rt, cfg, "from nav mode\x1b", "\r", gap=E.ESC_TIMEOUT_S + 0.3)
    assert [m.subject for m in sent(rt)] == ["from nav mode"]


def test_app_vi_k_walks_history(rt, cfg):
    run_keys(rt, cfg, "older\r", "x\x1b", "k", "\r")  # k at first line -> history
    assert [m.subject for m in sent(rt)] == ["older", "older"]


def test_app_reverse_search(rt, cfg):
    # C-r in vi insert mode, Enter accepts the search, second Enter submits it
    run_keys(rt, cfg, "alpha one\r", "beta two\r", "\x12alp", "\r", "\r")
    assert [m.subject for m in sent(rt)] == ["alpha one", "beta two", "alpha one"]
