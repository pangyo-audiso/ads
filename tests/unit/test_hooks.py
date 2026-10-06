"""`ads hook <event>` handlers (M2, plan §4.8). Payloads follow docs/spike-claude.md §4."""

from __future__ import annotations

import io
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from ads import hooks
from ads.bus import ledger as L
from ads.bus import state as S
from ads.bus import store
from ads.bus.envelope import pointer_line
from ads.config import default_config
from ads.paths import Runtime

REPO = Path(__file__).resolve().parents[2]
ADS = REPO / ".venv" / "bin" / "ads"

SID = "6f1c2a9e-0000-4000-8000-000000000001"
BASE = {"session_id": SID, "transcript_path": "/tmp/t.jsonl", "cwd": "/tmp/proj",
        "scratchpad_dir": "/tmp/scratch"}


def session_start(source: str = "startup") -> dict:
    p = {**BASE, "hook_event_name": "SessionStart", "source": source,
         "session_title": "ads-planner"}
    if source == "startup":
        p["model"] = "claude-sonnet-5-5"
    else:
        p.update(context_tokens=1234, estimated_cache_write_usd=0.01,
                 prompt_cache_likely_expired=False, seconds_since_last_response=12)
    return p


def prompt_submit(prompt: str) -> dict:
    return {**BASE, "prompt_id": "p-1", "permission_mode": "bypassPermissions",
            "hook_event_name": "UserPromptSubmit", "prompt": prompt, "session_title": "ads-planner"}


def stop(active: bool = False) -> dict:
    return {**BASE, "prompt_id": "p-1", "permission_mode": "bypassPermissions",
            "effort": {"level": "medium"}, "hook_event_name": "Stop", "stop_hook_active": active,
            "last_assistant_message": "done", "background_tasks": [], "session_crons": []}


def stop_failure(etype: str, msg: str, legacy: bool = False) -> dict:
    p = {**BASE, "hook_event_name": "StopFailure"}
    if legacy:
        p.update(error=etype, error_details=msg)
    else:
        p.update(error_type=etype, error_message=msg)
    return p


def session_end(reason: str) -> dict:
    return {**BASE, "prompt_id": "p-1", "hook_event_name": "SessionEnd", "reason": reason}


@pytest.fixture
def rt(tmp_runtime: Path) -> Runtime:
    r = Runtime(tmp_runtime)
    r.ensure()
    return r


def run_hook(rt: Runtime, event: str, payload: dict | str, agent: str = "planner"
             ) -> tuple[int, str]:
    """In-process: (exit code, stdout)."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    out = io.StringIO()
    code = hooks.main(event, stdin=io.StringIO(raw), stdout=out,
                      env={"ADS_RUNTIME": str(rt.root), "ADS_AGENT": agent})
    return code, out.getvalue()


def run_sub(rt: Runtime | None, event: str, raw: str, agent: str | None = "planner"
            ) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("ADS_RUNTIME", "ADS_AGENT")}
    if rt is not None:
        env["ADS_RUNTIME"] = str(rt.root)
    if agent is not None:
        env["ADS_AGENT"] = agent
    return subprocess.run([str(ADS), "hook", event], input=raw, capture_output=True, text=True,
                          env=env, timeout=30)


def log_lines(rt: Runtime) -> list[dict]:
    p = rt.logs / "hooks.log"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def errors(rt: Runtime) -> list[str]:
    return [r["error"] for r in log_lines(rt) if "error" in r]


def ctx(stdout: str) -> str:
    data = json.loads(stdout)
    return data["hookSpecificOutput"]["additionalContext"]


def delivered_task(rt: Runtime, to: str = "planner") -> str:
    """orchestrator -> `to` instruct, pasted and confirmed; returns its id."""
    m = L.send(rt, default_config(), from_="orchestrator", to=to, type="instruct",
               subject="Plan it", body="please plan")
    store.update(rt, m.id, status="delivering")
    L.mark_delivered(rt, m.id)
    return m.id


# --- base rules -------------------------------------------------------------------------

@pytest.mark.parametrize("missing", ["ADS_RUNTIME", "ADS_AGENT", "both"])
def test_unset_env_exits_0_silently(rt: Runtime, missing: str) -> None:
    cp = run_sub(None if missing in ("ADS_RUNTIME", "both") else rt, "session-start",
                 json.dumps(session_start()),
                 agent=None if missing in ("ADS_AGENT", "both") else "planner")
    assert cp.returncode == 0 and cp.stdout == "" and cp.stderr == ""
    assert not (rt.logs / "hooks.log").exists()


def test_broken_stdin_exit_0_traceback_logged(rt: Runtime) -> None:
    cp = run_sub(rt, "stop", "{not json")
    assert cp.returncode == 0 and cp.stdout == ""
    recs = log_lines(rt)
    assert recs[0]["event"] == "stop" and recs[0]["payload"] == {"_raw": "{not json"}
    assert any("Traceback" in e and "not JSON" in e for e in errors(rt))


def test_non_object_payload_and_unknown_event(rt: Runtime) -> None:
    assert run_hook(rt, "stop", "[1, 2]") == (0, "")
    assert run_hook(rt, "no-such-event", {}) == (0, "")
    assert len(errors(rt)) == 2


def test_handler_exception_logged_no_output(rt: Runtime, monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(S, "transition", boom)
    assert run_hook(rt, "session-start", session_start()) == (0, "")
    assert any("kaboom" in e for e in errors(rt))


def test_raw_event_logged_in_smoke_format(rt: Runtime) -> None:
    run_hook(rt, "session-end", session_end("other"))
    text = (rt.logs / "hooks.log").read_text()
    assert '"event": "session-end"' in text and f'"session_id": "{SID}"' in text


def test_empty_stdin_ok(rt: Runtime) -> None:
    cp = run_sub(rt, "session-end", "")
    assert cp.returncode == 0 and errors(rt) == []
    assert S.read_state(rt, "planner")["state"] == "down"


# --- session-start ----------------------------------------------------------------------

def test_session_start_context_counts_pending(rt: Runtime) -> None:
    cfg = default_config()
    L.send(rt, cfg, from_="orchestrator", to="planner", type="instruct", subject="a", body="b")
    L.send(rt, cfg, from_="orchestrator", to="planner", type="info", subject="c", body="d")
    L.send(rt, cfg, from_="orchestrator", to="developer", type="instruct", subject="e", body="f")
    cp = run_sub(rt, "session-start", json.dumps(session_start()))
    assert cp.returncode == 0
    out = json.loads(cp.stdout)
    assert out == {"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "ADS: you are planner. 2 message(s) pending."}}
    st = S.read_state(rt, "planner")
    assert st["state"] == "idle" and st["session_id"] == SID and st["seen_session_start"]


def test_session_start_resume_idle(rt: Runtime) -> None:
    code, out = run_hook(rt, "session-start", session_start("resume"))
    assert code == 0 and "0 message(s) pending" in ctx(out)
    assert S.read_state(rt, "planner")["state"] == "idle"


def test_session_start_compact_is_state_noop(rt: Runtime) -> None:
    run_hook(rt, "session-start", session_start())
    run_hook(rt, "prompt-submit", prompt_submit("hello"))
    before = S.read_state(rt, "planner")
    assert before["state"] == "busy"
    code, out = run_hook(rt, "session-start", session_start("compact"))
    after = S.read_state(rt, "planner")
    assert code == 0 and ctx(out).startswith("ADS: you are planner.")
    assert (after["state"], after["reason"], after["since"]) == (
        before["state"], before["reason"], before["since"])


# --- prompt-submit ----------------------------------------------------------------------

def test_prompt_submit_pointer_marks_delivered(rt: Runtime) -> None:
    run_hook(rt, "session-start", session_start())
    m = L.send(rt, default_config(), from_="orchestrator", to="planner", type="instruct",
               subject="Plan the thing", body="details")
    store.update(rt, m.id, status="delivering")
    S.transition(rt, "planner", "inflight", {"msg_id": m.id})
    cp = run_sub(rt, "prompt-submit", json.dumps(prompt_submit(pointer_line(m, rt))))
    assert cp.returncode == 0 and errors(rt) == []
    assert store.get(rt, m.id).status == "delivered"
    assert L.get_task(rt, m.id)["state"] == "delivered"
    st = S.read_state(rt, "planner")
    assert st["state"] == "busy" and st["inflight_msg"] is None and st["progress_this_turn"] == 0
    c = ctx(cp.stdout)
    assert json.loads(cp.stdout)["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert f"ADS message {m.id}" in c and "from: orchestrator" in c and "type: instruct" in c
    assert "subject: Plan the thing" in c and str(store.body_path(rt, m.id)) in c


def test_prompt_submit_pointer_supersedes_and_re(rt: Runtime) -> None:
    cfg = default_config()
    t1 = L.send(rt, cfg, from_="orchestrator", to="planner", type="instruct", subject="a", body="b")
    t2 = L.send(rt, cfg, from_="orchestrator", to="planner", type="instruct", subject="new",
                body="b", supersede=t1.id)
    code, out = run_hook(rt, "prompt-submit", prompt_submit(pointer_line(t2, rt)))
    assert code == 0 and f"supersedes: {t1.id}" in ctx(out)
    assert store.get(rt, t2.id).status == "delivered"


def test_prompt_submit_unknown_pointer(rt: Runtime) -> None:
    code, out = run_hook(rt, "prompt-submit", prompt_submit(
        "[ADS-MSG id=m-20261005-999999 from=orchestrator type=instruct] Read x"))
    assert code == 0 and "unknown message" in ctx(out)
    assert S.read_state(rt, "planner")["state"] == "busy" and errors(rt) == []


def test_prompt_submit_manual(rt: Runtime) -> None:
    run_hook(rt, "session-start", session_start())
    code, out = run_hook(rt, "prompt-submit", prompt_submit("please look at foo.py"))
    assert (code, out) == (0, "")
    assert S.read_state(rt, "planner")["state"] == "busy"
    assert any(r.get("note") == "manual" for r in log_lines(rt))


# --- stop -------------------------------------------------------------------------------

def test_stop_blocks_when_task_open_and_no_progress(rt: Runtime) -> None:
    tid = delivered_task(rt)
    run_hook(rt, "prompt-submit", prompt_submit("[ADS-MSG id=%s from=orchestrator type=instruct] x"
                                                 % tid))
    poke_before = rt.poke.stat().st_mtime_ns
    time.sleep(0.01)
    cp = run_sub(rt, "stop", json.dumps(stop()))
    assert cp.returncode == 0
    out = json.loads(cp.stdout)
    assert out["decision"] == "block" and tid in out["reason"]
    from ads.launcher import ads_bin
    assert f"{ads_bin(rt)} send --to orchestrator --type report --re {tid}" in out["reason"]
    assert S.read_state(rt, "planner")["state"] == "continuing"
    assert L.get_task(rt, tid)["nudges"] == 1
    assert rt.poke.stat().st_mtime_ns > poke_before


def test_stop_hook_active_still_capped_by_nudges(rt: Runtime) -> None:
    tid = delivered_task(rt)
    run_hook(rt, "prompt-submit", prompt_submit("work"))
    for _ in range(default_config().protocol.max_report_nudges):
        code, out = run_hook(rt, "stop", stop(active=True))
        assert json.loads(out)["decision"] == "block"
    code, out = run_hook(rt, "stop", stop(active=True))
    assert (code, out) == (0, "")
    assert S.read_state(rt, "planner")["state"] == "idle"
    assert L.get_task(rt, tid)["nudges"] == 2


def test_stop_allows_after_reply(rt: Runtime) -> None:
    tid = delivered_task(rt)
    run_hook(rt, "prompt-submit", prompt_submit("work"))
    L.send(rt, default_config(), from_="planner", to="orchestrator", type="report", re=tid,
           result="success", subject="done", body="ok")
    assert run_hook(rt, "stop", stop()) == (0, "")
    assert S.read_state(rt, "planner")["state"] == "idle"


def test_stop_allows_without_tasks(rt: Runtime) -> None:
    run_hook(rt, "session-start", session_start())
    run_hook(rt, "prompt-submit", prompt_submit("hi"))
    cp = run_sub(rt, "stop", json.dumps(stop()))
    assert cp.returncode == 0 and cp.stdout == ""
    assert S.read_state(rt, "planner")["state"] == "idle"
    assert rt.poke.exists()


def test_stop_allows_when_waiting_on_outgoing(rt: Runtime) -> None:
    delivered_task(rt)
    run_hook(rt, "prompt-submit", prompt_submit("work"))
    S.transition(rt, "planner", "prompt-submit", {})  # reset progress after the send below
    L.send(rt, default_config(), from_="planner", to="evaluator", type="review-request",
           subject="review", body="x")
    S.transition(rt, "planner", "prompt-submit", {})
    assert run_hook(rt, "stop", stop()) == (0, "")


# --- stop-failure -----------------------------------------------------------------------

@pytest.mark.parametrize("legacy", [False, True])
def test_stop_failure_unrecoverable_writes_alert(rt: Runtime, legacy: bool) -> None:
    code, out = run_hook(rt, "stop-failure",
                         stop_failure("billing_error", "credit exhausted", legacy=legacy))
    assert (code, out) == (0, "")
    st = S.read_state(rt, "planner")
    assert st["state"] == "idle" and st["reason"] == "stop-failure"
    assert st["last_error"] == {"type": "billing_error", "message": "credit exhausted",
                                "alert": True}
    (alert,) = list(rt.alerts.glob("*-planner.json"))
    assert json.loads(alert.read_text())["error_type"] == "billing_error"
    assert json.loads(alert.read_text())["error_message"] == "credit exhausted"
    assert rt.poke.exists()


def test_stop_failure_recoverable_no_alert(rt: Runtime) -> None:
    run_hook(rt, "stop-failure", stop_failure("rate_limit", "slow down"))
    assert S.read_state(rt, "planner")["last_error"]["alert"] is False
    assert list(rt.alerts.glob("*.json")) == []


# --- session-end ------------------------------------------------------------------------

@pytest.mark.parametrize("reason,state", [
    ("clear", "restarting"), ("resume", "restarting"), ("logout", "down"),
    ("prompt_input_exit", "down"), ("other", "down"),
])
def test_session_end(rt: Runtime, reason: str, state: str) -> None:
    run_hook(rt, "session-start", session_start())
    assert run_hook(rt, "session-end", session_end(reason)) == (0, "")
    st = S.read_state(rt, "planner")
    assert (st["state"], st["reason"]) == (state, reason)


def test_session_end_subprocess_fast(rt: Runtime) -> None:
    raw = json.dumps(session_end("other"))
    run_sub(rt, "session-end", raw)  # warm caches
    t0 = time.monotonic()
    cp = run_sub(rt, "session-end", raw)
    elapsed = time.monotonic() - t0
    assert cp.returncode == 0 and cp.stdout == ""
    assert elapsed < 1.0, f"session-end took {elapsed:.3f}s"
