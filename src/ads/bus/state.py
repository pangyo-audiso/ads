"""Per-agent state machine: `work/state/<agent>.json` (plan §4.5).

`apply()` is a pure function implementing the state table; `transition()` wraps it in a
`locked_json` read-modify-write. Stdlib only (hooks import this).

Events
------
Hook events (payload = the hook's stdin JSON):
  session-start   source ∈ startup|resume|clear|fork → idle (progress=0); compact → unchanged
  prompt-submit   → busy; progress=0; inflight=None
  stop            payload["decision"] == "block" → continuing, else → idle
  stop-failure    → idle; last_error = {type, message, alert}; alert iff type ∈ UNRECOVERABLE
  session-end     reason ∈ logout|prompt_input_exit|other (or unknown) → down(reason)
                  clear|resume → restarting(reason)
  In down(shutdown) every hook event is ignored: `ads stop` killed the pane and late hooks
  (Stop/SessionEnd racing the kill) must not resurrect it. Only `respawn` leaves it.
Supervisor events:
  respawn             → starting (seen_session_start=False, progress=0, inflight=None)
  pane_dead           → down(pane_dead) from ANY state
  shutdown            → down(shutdown)
  dialog_unknown      → dialog(payload["name"] or "unknown")
  dialog_cleared      → idle if seen_session_start else starting   (only while in `dialog`)
  restarting_timeout  → down(no-restart)                            (only while `restarting`)
  stale_idle          → idle(stale)                                 (only while busy/continuing)
  inflight            → sets inflight_msg = payload["msg_id"] (may be None); state unchanged
`ads send`:
  progress            → progress_this_turn += 1

The guarded supervisor events are no-ops outside their source state, so a hook that wins a
race (e.g. SessionStart just before the restarting timeout) is never overwritten.

Idempotency: replaying any event on its own result gives the same result (`since` only
moves when state/reason change). The one deliberate exception is `progress`, which is an
additive counter: every reply/task-creating send increments it.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ads.paths import AGENTS, ProjectState, StateLike, as_state, locked_json

STATES: frozenset[str] = frozenset({
    "starting", "idle", "busy", "continuing", "dialog", "restarting", "down",
})

HOOK_EVENTS: frozenset[str] = frozenset({
    "session-start", "prompt-submit", "stop", "stop-failure", "session-end",
})
SUPERVISOR_EVENTS: frozenset[str] = frozenset({
    "respawn", "pane_dead", "shutdown", "dialog_unknown", "dialog_cleared",
    "restarting_timeout", "stale_idle", "inflight",
})
EVENTS: frozenset[str] = HOOK_EVENTS | SUPERVISOR_EVENTS | {"progress"}

UNRECOVERABLE_ERRORS: frozenset[str] = frozenset({
    "authentication_failed", "oauth_org_not_allowed", "account_on_hold",
    "billing_error", "model_not_found", "invalid_request",
})

SESSION_START_IDLE = frozenset({"startup", "resume", "clear", "fork"})
SESSION_END_RESTARTING = frozenset({"clear", "resume"})


def default_state() -> dict[str, Any]:
    """State of an agent that has never been launched."""
    return {
        "state": "down",
        "reason": "not-started",
        "since": None,
        "session_id": None,
        "last_event": None,
        "progress_this_turn": 0,
        "inflight_msg": None,
        "last_error": None,
        "seen_session_start": False,
    }


def is_deliverable(st: dict[str, Any] | None) -> bool:
    """Only an `idle` agent may receive a paste."""
    return bool(st) and st.get("state") == "idle"


def error_fields(payload: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """(type, message) from a StopFailure payload; accepts both documented and legacy names."""
    p = payload or {}
    etype = p.get("error_type") or p.get("error")
    emsg = p.get("error_message") or p.get("error_details")
    if etype is not None and not isinstance(etype, str):
        etype = json.dumps(etype, default=str)
    if emsg is not None and not isinstance(emsg, str):
        emsg = json.dumps(emsg, default=str)
    return etype, emsg


def alert_needed(payload: dict[str, Any] | None) -> bool:
    """True when a StopFailure payload names an unrecoverable error type."""
    etype, _ = error_fields(payload)
    return etype in UNRECOVERABLE_ERRORS


def _iso(now: datetime | str | None) -> str:
    if now is None:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    if isinstance(now, str):
        return now
    return now.isoformat(timespec="seconds")


def apply(state: dict[str, Any] | None, event: str, payload: dict[str, Any] | None = None,
          now: datetime | str | None = None) -> dict[str, Any]:
    """Pure transition: return a new state dict for `event` (the input is not mutated).

    Raises ValueError for an unknown event.
    """
    if event not in EVENTS:
        raise ValueError(f"unknown state event: {event!r}")
    p = payload or {}
    base = default_state()
    base.update(copy.deepcopy(state or {}))
    new = base
    ts = _iso(now)

    def goto(st: str, reason: str | None) -> None:
        if new["state"] != st or new.get("reason") != reason:
            new["since"] = ts
        new["state"] = st
        new["reason"] = reason

    if event in HOOK_EVENTS and new["state"] == "down" and new.get("reason") == "shutdown":
        new["last_event"] = event  # late hook after `ads stop`: record it, keep down(shutdown)
        return new

    if event in HOOK_EVENTS:
        sid = p.get("session_id")
        if sid:
            new["session_id"] = sid

    if event == "session-start":
        source = p.get("source") or "startup"
        new["seen_session_start"] = True
        if source != "compact":
            goto("idle", source)
            new["progress_this_turn"] = 0
    elif event == "prompt-submit":
        goto("busy", None)
        new["progress_this_turn"] = 0
        new["inflight_msg"] = None
    elif event == "stop":
        if p.get("decision") == "block":
            goto("continuing", "stop-block")
        else:
            goto("idle", None)
    elif event == "stop-failure":
        etype, emsg = error_fields(p)
        goto("idle", "stop-failure")
        new["last_error"] = {"type": etype, "message": emsg,
                             "alert": etype in UNRECOVERABLE_ERRORS}
    elif event == "session-end":
        reason = p.get("reason") or "other"
        if reason in SESSION_END_RESTARTING:
            goto("restarting", reason)
        else:
            goto("down", reason)
    elif event == "respawn":
        goto("starting", p.get("reason") or "respawn")
        new["seen_session_start"] = False
        new["progress_this_turn"] = 0
        new["inflight_msg"] = None
    elif event == "pane_dead":
        goto("down", "pane_dead")
    elif event == "shutdown":
        goto("down", "shutdown")
    elif event == "dialog_unknown":
        goto("dialog", p.get("name") or "unknown")
    elif event == "dialog_cleared":
        if new["state"] == "dialog":
            goto("idle" if new.get("seen_session_start") else "starting", "dialog-cleared")
    elif event == "restarting_timeout":
        if new["state"] == "restarting":
            goto("down", "no-restart")
    elif event == "stale_idle":
        if new["state"] in ("busy", "continuing"):
            goto("idle", "stale")
    elif event == "inflight":
        new["inflight_msg"] = p.get("msg_id")
    elif event == "progress":
        new["progress_this_turn"] = int(new.get("progress_this_turn") or 0) + 1

    new["last_event"] = event
    return new


# --- file wrapper ---------------------------------------------------------------------

def _rt(ps: StateLike) -> ProjectState:
    return as_state(ps)


def read_state(ps: StateLike, agent: str) -> dict[str, Any]:
    """Current state (default_state() if the file is missing or unreadable)."""
    st = default_state()
    try:
        data = json.loads(_rt(ps).state_file(agent).read_text() or "{}")
        if isinstance(data, dict):
            st.update(data)
    except (FileNotFoundError, ValueError):
        pass
    return st


def all_states(ps: StateLike) -> dict[str, dict[str, Any]]:
    """{agent: state} for every agent in canonical order."""
    return {a: read_state(ps, a) for a in AGENTS}


def transition(ps: StateLike, agent: str, event: str,
               payload: dict[str, Any] | None = None,
               now: datetime | str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Locked read-apply-write of `work/state/<agent>.json`. Returns (old, new)."""
    if event not in EVENTS:
        raise ValueError(f"unknown state event: {event!r}")
    path = _rt(ps).state_file(agent)
    with locked_json(path) as data:
        old = default_state()
        old.update(copy.deepcopy(data))
        new = apply(old, event, payload, now)
        data.clear()
        data.update(new)
    return old, new
