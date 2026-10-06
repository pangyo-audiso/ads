"""Claude Code hook handlers behind `ads hook <event>` (plan §4.8).

Stdlib + `ads.paths` / `ads.projects` / `ads.bus.*` only: this runs on every Claude Code event, and
SessionEnd has a 1 s timeout. Rules:
- `$ADS_AGENT` and the project state must both be known, else exit 0 silently. The state is
  `$ADS_STATE_DIR` (an existing dir, `<runtime>/projects/<name>`), else the project
  `$ADS_PROJECT` registered in `$ADS_RUNTIME`.
- The raw event is always appended to `<state>/work/logs/hooks.log` first (debugging; smoke test).
- Any exception is appended to hooks.log as a traceback record; the exit code is always 0
  and nothing is printed for a failed handler.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from ads.bus import ledger
from ads.bus import state as agent_state
from ads.bus import store
from ads.bus.envelope import parse_pointer
from ads.paths import ProjectState, StateLike, as_state

HOOK_EVENT_NAMES: dict[str, str] = {
    "session-start": "SessionStart",
    "prompt-submit": "UserPromptSubmit",
    "stop": "Stop",
    "stop-failure": "StopFailure",
    "session-end": "SessionEnd",
}


# --- logging ----------------------------------------------------------------------------

def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _append(rt: ProjectState, record: dict[str, Any]) -> None:
    """Append one JSON line to hooks.log (O_APPEND, single write)."""
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    log = rt.logs / "hooks.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def _note(rt: ProjectState, agent: str, event: str, note: str, **extra: Any) -> None:
    _append(rt, {"ts": _ts(), "agent": agent, "event": event, "note": note, **extra})


def _context(event: str, text: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": HOOK_EVENT_NAMES[event],
                                              "additionalContext": text}}, ensure_ascii=False)


# --- handlers (each returns the stdout text, or None) -----------------------------------

def on_session_start(rt: ProjectState, agent: str, payload: dict[str, Any]) -> str | None:
    agent_state.transition(rt, agent, "session-start", payload)
    n = sum(1 for m in store.all_messages(rt)
            if m.to == agent and m.status in ("queued", "held"))
    return _context("session-start", f"ADS: you are {agent}. {n} message(s) pending.")


def _front_matter(rt: ProjectState, msg_id: str, ptr: dict[str, Any]) -> str:
    try:
        m = store.get(rt, msg_id)
    except store.MessageNotFound:
        return f"ADS: unknown message {msg_id} (no envelope in {rt.msgs})."
    lines = [f"ADS message {m.id}", f"from: {m.from_}", f"type: {m.type}"]
    if m.re:
        lines.append(f"re: {m.re}")
    if m.parent:
        lines.append(f"parent: {m.parent}")
    if m.result:
        lines.append(f"result: {m.result}")
    lines.append(f"subject: {m.subject}")
    if m.supersedes or ptr.get("supersedes"):
        lines.append(f"supersedes: {m.supersedes or ptr['supersedes']} (abort that task first)")
    lines.append(f"body: {store.body_path(rt, m.id)}")
    return "\n".join(lines)


def on_prompt_submit(rt: ProjectState, agent: str, payload: dict[str, Any]) -> str | None:
    prompt = payload.get("prompt")
    ptr = parse_pointer(prompt if isinstance(prompt, str) else None)
    if ptr is None:
        agent_state.transition(rt, agent, "prompt-submit", payload)
        _note(rt, agent, "prompt-submit", "manual")
        return None
    msg = ledger.mark_delivered(rt, ptr["id"])
    agent_state.transition(rt, agent, "prompt-submit", payload)
    if msg is None:
        _note(rt, agent, "prompt-submit", "unknown-pointer", msg_id=ptr["id"])
    elif msg.to != agent:
        _note(rt, agent, "prompt-submit", "pointer-for-other-agent", msg_id=msg.id, to=msg.to)
    else:
        _note(rt, agent, "prompt-submit", "delivered", msg_id=msg.id, status=msg.status)
    return _context("prompt-submit", _front_matter(rt, ptr["id"], ptr))


def _load_cfg(rt: ProjectState) -> Any:
    from ads.config import ConfigError, default_config, load_config
    try:
        return load_config(runtime=rt.runtime)
    except ConfigError:
        _note(rt, os.environ.get("ADS_AGENT", "?"), "config", "invalid ads.toml; using defaults")
        return default_config()


def on_stop(rt: ProjectState, agent: str, payload: dict[str, Any]) -> str | None:
    reason = ledger.stop_decision(rt, _load_cfg(rt), agent)
    decision = "block" if reason else "allow"
    agent_state.transition(rt, agent, "stop", {**payload, "decision": decision})
    ledger.touch_poke(rt)
    if reason:
        _note(rt, agent, "stop", "block", stop_hook_active=payload.get("stop_hook_active"))
        return json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False)
    return None


def on_stop_failure(rt: ProjectState, agent: str, payload: dict[str, Any]) -> str | None:
    agent_state.transition(rt, agent, "stop-failure", payload)
    if agent_state.alert_needed(payload):
        etype, emsg = agent_state.error_fields(payload)
        rt.alerts.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        path = rt.alerts / f"{stamp}-{agent}.json"
        tmp = path.with_name("." + path.name + ".tmp")
        tmp.write_text(json.dumps({"agent": agent, "error_type": etype, "error_message": emsg,
                                   "ts": _ts()}, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    ledger.touch_poke(rt)
    return None


def on_session_end(rt: ProjectState, agent: str, payload: dict[str, Any]) -> str | None:
    agent_state.transition(rt, agent, "session-end", payload)
    return None


HANDLERS: dict[str, Callable[[ProjectState, str, dict[str, Any]], str | None]] = {
    "session-start": on_session_start,
    "prompt-submit": on_prompt_submit,
    "stop": on_stop,
    "stop-failure": on_stop_failure,
    "session-end": on_session_end,
}


# --- entry point ------------------------------------------------------------------------

def state_from_env(env: Any) -> ProjectState | None:
    """$ADS_STATE_DIR (an existing dir); else the project $ADS_PROJECT registered in
    $ADS_RUNTIME; else None."""
    sd = env.get("ADS_STATE_DIR")
    if sd:
        return ProjectState.at(sd) if Path(sd).is_dir() else None
    runtime, project = env.get("ADS_RUNTIME"), env.get("ADS_PROJECT")
    if runtime and project and Path(runtime).is_dir():
        from ads.projects import find_by_path
        return find_by_path(runtime, project)
    return None


def run(event: str, raw: str, state_dir: str | Path | ProjectState, agent: str) -> str | None:
    """Log the raw event, dispatch, and return stdout text. Never raises."""
    rt = as_state(state_dir)
    try:
        try:
            payload: Any = json.loads(raw) if raw.strip() else {}
        except ValueError:
            payload = None
        _append(rt, {"ts": _ts(), "agent": agent, "event": event,
                     "payload": payload if payload is not None else {"_raw": raw}})
        if payload is None:
            raise ValueError(f"hook stdin is not JSON: {raw[:200]!r}")
        if not isinstance(payload, dict):
            raise ValueError(f"hook stdin is not a JSON object: {type(payload).__name__}")
        handler = HANDLERS.get(event)
        if handler is None:
            raise ValueError(f"unknown hook event {event!r}")
        return handler(rt, agent, payload)
    except BaseException:  # a hook must never fail the Claude session
        try:
            _append(rt, {"ts": _ts(), "agent": agent, "event": event,
                         "error": traceback.format_exc()})
        except BaseException:
            pass
        return None


def main(event: str, stdin: TextIO | None = None, stdout: TextIO | None = None,
         env: dict[str, str] | None = None) -> int:
    """`ads hook <event>`: always returns 0."""
    try:
        env = os.environ if env is None else env  # type: ignore[assignment]
        agent = env.get("ADS_AGENT")
        state = state_from_env(env)
        if not agent or state is None:
            return 0
        stdin = sys.stdin if stdin is None else stdin
        try:
            raw = "" if stdin is None or stdin.isatty() else stdin.read()
        except (OSError, ValueError, UnicodeDecodeError) as e:
            raw = f"<unreadable stdin: {e}>"
        out = run(event, raw, state, agent)
        if out:
            stdout = sys.stdout if stdout is None else stdout
            stdout.write(out + "\n")
            stdout.flush()
    except BaseException:
        pass
    return 0
