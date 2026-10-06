"""`ads send|status|note` CLI (M2, plan §5, §8, §4.9)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ads import cli
from ads.bus import ledger as L
from ads.bus import state as S
from ads.bus import store
from ads.paths import ProjectState

REPO = Path(__file__).resolve().parents[2]
ADS = REPO / ".venv" / "bin" / "ads"


@pytest.fixture
def rt(tmp_state: ProjectState, monkeypatch: pytest.MonkeyPatch) -> ProjectState:
    """The only registered project of the runtime: selected without -p or env."""
    monkeypatch.setenv("ADS_RUNTIME", str(tmp_state.runtime))
    monkeypatch.delenv("ADS_AGENT", raising=False)
    return tmp_state


def ads(*argv: str, capsys) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def sub(rt: ProjectState, *argv: str, stdin: str | None = None, agent: str | None = None
        ) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "ADS_AGENT"}
    env["ADS_RUNTIME"] = str(rt.runtime)
    if agent:
        env["ADS_AGENT"] = agent
    return subprocess.run([str(ADS), *argv], input=stdin, capture_output=True, text=True, env=env,
                          timeout=30)


# --- send -------------------------------------------------------------------------------

def test_send_prints_id_and_writes_message(rt: ProjectState, capsys) -> None:
    code, out, err = ads("send", "--from", "human", "--to", "orchestrator", "--type", "instruct",
                         "--subject", "Build it", "--body", "do the thing", capsys=capsys)
    assert code == 0 and err == ""
    mid = out.strip()
    m = store.get(rt, mid)
    assert (m.from_, m.to, m.type, m.status, m.subject) == (
        "human", "orchestrator", "instruct", "queued", "Build it")
    assert store.read_body(rt, mid) == "do the thing\n"
    assert L.get_task(rt, mid)["state"] == "queued"


def test_send_from_defaults_to_env_and_body_stdin(rt: ProjectState) -> None:
    cp = sub(rt, "send", "--to", "planner", "--type", "info", "--subject", "fyi",
             "--body-file", "-", stdin="from stdin\n", agent="orchestrator")
    assert cp.returncode == 0, cp.stderr
    m = store.get(rt, cp.stdout.strip())
    assert m.from_ == "orchestrator" and store.read_body(rt, m.id) == "from stdin\n"
    cp = sub(rt, "send", "--to", "planner", "--type", "info", "--subject", "x", "--body", "-",
             stdin="dash body", agent="orchestrator")
    assert cp.returncode == 0 and store.read_body(rt, cp.stdout.strip()) == "dash body\n"


def test_send_body_file(rt: ProjectState, tmp_path: Path, capsys) -> None:
    f = tmp_path / "body.md"
    f.write_text("# Plan\nline\n")
    code, out, _ = ads("send", "--from", "human", "--to", "planner", "--type", "info",
                       "--subject", "s", "--body-file", str(f), capsys=capsys)
    assert code == 0 and store.read_body(rt, out.strip()) == "# Plan\nline\n"


def test_send_held_reports_holder(rt: ProjectState, capsys) -> None:
    _, first, _ = ads("send", "--from", "orchestrator", "--to", "planner", "--type", "instruct",
                      "--subject", "a", "--body", "b", capsys=capsys)
    code, out, _ = ads("send", "--from", "orchestrator", "--to", "developer", "--type",
                       "instruct", "--subject", "c", "--body", "d", capsys=capsys)
    assert code == 0
    mid, word1, word2, holder = out.split()
    assert (word1, word2, holder) == ("held", "behind", first.strip())
    assert store.get(rt, mid).status == "held"


@pytest.mark.parametrize("argv,needle", [
    (["--to", "planner", "--type", "info", "--subject", "s", "--body", "b"], "--from"),
    (["--from", "human", "--to", "nobody", "--type", "info", "--subject", "s", "--body", "b"],
     "unknown recipient"),
    (["--from", "human", "--to", "planner", "--type", "info", "--subject", "s"], "body"),
    (["--from", "planner", "--to", "orchestrator", "--type", "report", "--result", "success",
      "--subject", "s", "--body", "b"], "--re"),
    (["--from", "human", "--to", "planner", "--type", "info", "--subject", "s",
      "--body-file", "/nonexistent/x.md"], "--body-file"),
])
def test_send_errors_exit_1(rt: ProjectState, capsys, argv, needle) -> None:
    code, out, err = ads("send", *argv, capsys=capsys)
    assert code == 1 and out == "" and needle in err
    assert store.all_messages(rt) == []


def test_send_reply_closes_task_and_bumps_progress_once(rt: ProjectState, capsys) -> None:
    _, out, _ = ads("send", "--from", "orchestrator", "--to", "planner", "--type", "instruct",
                    "--subject", "plan", "--body", "b", capsys=capsys)
    tid = out.strip()
    S.transition(rt, "planner", "prompt-submit", {})
    cp = sub(rt, "send", "--to", "orchestrator", "--type", "report", "--re", tid, "--result",
             "success", "--subject", "done", "--body", "ok", agent="planner")
    assert cp.returncode == 0, cp.stderr
    assert L.get_task(rt, tid)["state"] == "closed"
    assert S.read_state(rt, "planner")["progress_this_turn"] == 1


def test_requeue_failed(rt: ProjectState, capsys) -> None:
    _, out, _ = ads("send", "--from", "human", "--to", "planner", "--type", "info",
                    "--subject", "s", "--body", "b", capsys=capsys)
    mid = out.strip()
    store.update(rt, mid, status="delivering")
    store.bump(rt, mid, "pastes", 3)
    store.update(rt, mid, status="failed")
    rt.poke.unlink(missing_ok=True)
    code, out, _ = ads("send", "--requeue", mid, capsys=capsys)
    assert code == 0 and mid in out
    m = store.get(rt, mid)
    assert m.status == "queued" and m.pastes == 0 and m.enters == 0
    assert rt.poke.exists()


def test_requeue_rejects_non_failed_and_unknown(rt: ProjectState, capsys) -> None:
    _, out, _ = ads("send", "--from", "human", "--to", "planner", "--type", "info",
                    "--subject", "s", "--body", "b", capsys=capsys)
    code, _, err = ads("send", "--requeue", out.strip(), capsys=capsys)
    assert code == 1 and "only failed" in err
    code, _, err = ads("send", "--requeue", "m-20261005-999999", capsys=capsys)
    assert code == 1 and "no such message" in err


# --- status -----------------------------------------------------------------------------

def test_status_json_structure(rt: ProjectState, capsys) -> None:
    S.transition(rt, "planner", "session-start", {"source": "startup"})
    _, out, _ = ads("send", "--from", "orchestrator", "--to", "planner", "--type", "instruct",
                    "--subject", "plan", "--body", "b", capsys=capsys)
    t1 = out.strip()
    _, out, _ = ads("send", "--from", "orchestrator", "--to", "developer", "--type", "instruct",
                    "--subject", "dev", "--body", "b", capsys=capsys)
    held = out.split()[0]
    _, out, _ = ads("send", "--from", "orchestrator", "--to", "planner", "--type", "instruct",
                    "--subject", "plan v2", "--body", "b", "--supersede", t1, capsys=capsys)
    t2 = out.strip()
    (rt.alerts / "20261005T000000-tester.json").write_text(json.dumps(
        {"agent": "tester", "error_type": "billing_error", "error_message": "x"}))
    rt.supervisor_pid.write_text(f"{os.getpid()}\n")

    cp = sub(rt, "status", "--json")
    assert cp.returncode == 0, cp.stderr
    d = json.loads(cp.stdout)
    assert list(d["agents"]) == ["orchestrator", "planner", "tester", "evaluator", "developer",
                                 "coder-1", "coder-2"]
    assert all("state" in a for a in d["agents"].values())
    p = d["agents"]["planner"]
    assert p["state"] == "idle" and p["queued"] == 1 and p["model"]
    assert d["agents"]["developer"]["queued"] + d["agents"]["developer"]["held"] == 1
    ids = {t["id"]: t for t in d["tasks"]}
    assert t2 in ids and t1 not in ids and ids[t2]["nudges"] == 0
    assert {"id", "from", "to", "type", "state", "nudges"} <= set(ids[t2])
    assert d["supersede_pending"] == [{"task": t1, "replacement": t2, "to": "planner",
                                       "status": "queued"}]
    assert d["phase"] in ("plan", "dev")
    assert d["supervisor"]["alive"] is True and d["supervisor"]["pid"] == os.getpid()
    assert d["alerts"][0]["error_type"] == "billing_error"
    assert any(m["id"] == held for m in d["messages"])


def test_status_human_readable(rt: ProjectState, capsys) -> None:
    ads("send", "--from", "orchestrator", "--to", "planner", "--type", "instruct",
        "--subject", "plan", "--body", "b", capsys=capsys)
    ads("send", "--from", "orchestrator", "--to", "developer", "--type", "instruct",
        "--subject", "dev", "--body", "b", capsys=capsys)
    code, out, _ = ads("status", capsys=capsys)
    assert code == 0
    assert "phase: plan" in out and "supervisor: not running" in out
    assert "orchestrator->planner" in out and "held by" in out
    for agent in ("orchestrator", "coder-2"):
        assert agent in out


def test_status_dead_supervisor(rt: ProjectState) -> None:
    rt.supervisor_pid.write_text("999999999\n")
    d = json.loads(sub(rt, "status", "--json").stdout)
    assert d["supervisor"] == {"pid": 999999999, "alive": False,
                               "pid_file": str(rt.supervisor_pid)}


# --- note -------------------------------------------------------------------------------

def test_note_appends_under_lab_notes(rt: ProjectState) -> None:
    rt.claude_md.write_text("# ProjectState\n\n## Lab Notes\n<!-- comment -->\n- [2026-01-01 human] old\n"
                            "\n## Other\nkeep me\n")
    cp = sub(rt, "note", "multi\nline  note", agent="planner")
    assert cp.returncode == 0, cp.stderr
    text = rt.claude_md.read_text()
    lines = text.splitlines()
    i = lines.index("- [2026-01-01 human] old")
    assert lines[i + 1].startswith("- [") and lines[i + 1].endswith(" planner] multi line note")
    assert text.endswith("## Other\nkeep me\n")
    cp = sub(rt, "note", "by hand")
    assert "human] by hand" in rt.claude_md.read_text()


def test_note_creates_section_and_file(rt: ProjectState) -> None:
    rt.claude_md.unlink(missing_ok=True)
    assert sub(rt, "note", "hello").returncode == 0
    lines = rt.claude_md.read_text().splitlines()
    assert lines[0] == "## Lab Notes" and lines[1].endswith("human] hello")
    rt.claude_md.write_text("# Title\nbody\n")
    assert sub(rt, "note", "again").returncode == 0
    assert rt.claude_md.read_text().startswith("# Title\nbody\n\n## Lab Notes\n- [")


def test_note_warns_over_80(rt: ProjectState) -> None:
    rt.claude_md.write_text("## Lab Notes\n" + "".join(f"- [2026-01-01 human] n{i}\n"
                                                        for i in range(80)))
    cp = sub(rt, "note", "one more")
    assert cp.returncode == 0 and "81 entries" in cp.stderr and "warning" in cp.stderr


def test_note_concurrent(rt: ProjectState) -> None:
    rt.claude_md.write_text("## Lab Notes\n")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda i: cli.add_lab_note(rt.claude_md, rt.run / "claude-md.lock",
                                               "human", f"n{i}"), range(40)))
    entries = [ln for ln in rt.claude_md.read_text().splitlines() if ln.startswith("- [")]
    assert len(entries) == 40


# --- misc -------------------------------------------------------------------------------

def test_hook_dispatch_via_cli_main(rt: ProjectState, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ADS_AGENT", "planner")
    monkeypatch.setenv("ADS_STATE_DIR", str(rt.dir))
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO('{"reason": "logout"}'))
    assert cli.main(["hook", "session-end"]) == 0
    assert S.read_state(rt, "planner")["reason"] == "logout"


def test_attach_without_session(rt: ProjectState, capsys) -> None:
    code, _, err = ads("attach", capsys=capsys)
    assert code == 1 and "no ads session recorded" in err


def test_stop_without_session(rt: ProjectState, capsys) -> None:
    code, _, err = ads("stop", capsys=capsys)
    assert code == 1 and "no ads session recorded" in err


def test_restart_agent_writes_request(rt: ProjectState, capsys) -> None:
    import json
    code, out, err = ads("restart", "planner", "--resume", capsys=capsys)
    assert code == 0 and "restart requested: planner --resume" in out
    assert "supervisor is not running" in err
    (req,) = list(rt.requests.glob("*.json"))
    data = json.loads(req.read_text())
    assert data["op"] == "restart" and data["agent"] == "planner" and data["resume"] is True
    code, _, err = ads("restart", "nobody", capsys=capsys)
    assert code == 1 and "unknown target" in err
