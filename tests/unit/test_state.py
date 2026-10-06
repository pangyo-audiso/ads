"""Agent state machine (M1b, plan §4.5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ads.bus import state as S
from ads.paths import ProjectState

T0 = "2026-10-05T10:00:00+09:00"
T1 = "2026-10-05T10:05:00+09:00"


def st(state: str, **kw) -> dict:
    d = S.default_state()
    d.update(state=state, reason=kw.pop("reason", None), since=T0)
    d.update(kw)
    return d


# (start state dict, event, payload, expected subset)
ROWS = [
    # SessionStart
    (st("starting", progress_this_turn=3), "session-start", {"source": "startup", "session_id": "u1"},
     {"state": "idle", "reason": "startup", "progress_this_turn": 0, "seen_session_start": True,
      "session_id": "u1"}),
    (st("restarting"), "session-start", {"source": "resume"}, {"state": "idle", "reason": "resume"}),
    (st("restarting"), "session-start", {"source": "clear"}, {"state": "idle", "reason": "clear"}),
    (st("starting"), "session-start", {"source": "fork"}, {"state": "idle", "reason": "fork"}),
    (st("dialog", reason="x"), "session-start", {"source": "startup"}, {"state": "idle"}),
    (st("busy", progress_this_turn=2), "session-start", {"source": "compact"},
     {"state": "busy", "progress_this_turn": 2, "since": T0, "last_event": "session-start"}),
    # UserPromptSubmit
    (st("idle", progress_this_turn=4, inflight_msg="m-20261005-000001"), "prompt-submit", {},
     {"state": "busy", "progress_this_turn": 0, "inflight_msg": None}),
    (st("dialog"), "prompt-submit", {}, {"state": "busy"}),
    # Stop
    (st("busy"), "stop", {"decision": "allow"}, {"state": "idle"}),
    (st("busy"), "stop", {}, {"state": "idle"}),
    (st("busy"), "stop", {"decision": "block"}, {"state": "continuing"}),
    (st("continuing"), "stop", {"decision": "allow"}, {"state": "idle"}),
    # StopFailure
    (st("busy"), "stop-failure", {"error_type": "rate_limit", "error_message": "slow down"},
     {"state": "idle", "last_error": {"type": "rate_limit", "message": "slow down", "alert": False}}),
    (st("busy"), "stop-failure", {"error": "billing_error", "error_details": "pay"},
     {"state": "idle", "last_error": {"type": "billing_error", "message": "pay", "alert": True}}),
    # SessionEnd
    (st("idle"), "session-end", {"reason": "logout"}, {"state": "down", "reason": "logout"}),
    (st("idle"), "session-end", {"reason": "prompt_input_exit"},
     {"state": "down", "reason": "prompt_input_exit"}),
    (st("idle"), "session-end", {"reason": "other"}, {"state": "down", "reason": "other"}),
    (st("idle"), "session-end", {}, {"state": "down", "reason": "other"}),
    (st("down", reason="shutdown"), "session-end", {"reason": "other"},
     {"state": "down", "reason": "shutdown"}),
    # late hooks after `ads stop` never resurrect the agent
    (st("down", reason="shutdown"), "stop", {}, {"state": "down", "reason": "shutdown"}),
    (st("down", reason="shutdown"), "stop-failure", {"error_type": "rate_limit"},
     {"state": "down", "reason": "shutdown"}),
    (st("down", reason="shutdown"), "prompt-submit", {"prompt": "x"},
     {"state": "down", "reason": "shutdown"}),
    (st("down", reason="shutdown"), "respawn", {}, {"state": "starting", "reason": "respawn"}),
    (st("idle"), "session-end", {"reason": "clear"}, {"state": "restarting", "reason": "clear"}),
    (st("busy"), "session-end", {"reason": "resume"}, {"state": "restarting", "reason": "resume"}),
    # supervisor
    (st("down", reason="pane_dead", seen_session_start=True, inflight_msg="m-20261005-000002",
        progress_this_turn=1), "respawn", {},
     {"state": "starting", "seen_session_start": False, "inflight_msg": None, "progress_this_turn": 0}),
    (st("idle"), "shutdown", {}, {"state": "down", "reason": "shutdown"}),
    (st("starting"), "dialog_unknown", {"name": "mystery"}, {"state": "dialog", "reason": "mystery"}),
    (st("starting"), "dialog_unknown", {}, {"state": "dialog", "reason": "unknown"}),
    (st("dialog"), "dialog_cleared", {}, {"state": "starting"}),
    (st("dialog", seen_session_start=True), "dialog_cleared", {}, {"state": "idle"}),
    (st("busy"), "dialog_cleared", {}, {"state": "busy", "since": T0}),
    (st("restarting"), "restarting_timeout", {}, {"state": "down", "reason": "no-restart"}),
    (st("idle"), "restarting_timeout", {}, {"state": "idle", "since": T0}),
    (st("busy"), "stale_idle", {}, {"state": "idle", "reason": "stale"}),
    (st("continuing"), "stale_idle", {}, {"state": "idle", "reason": "stale"}),
    (st("starting"), "stale_idle", {}, {"state": "starting"}),
    (st("idle"), "inflight", {"msg_id": "m-20261005-000003"},
     {"state": "idle", "inflight_msg": "m-20261005-000003", "since": T0}),
    (st("busy", progress_this_turn=1), "progress", {}, {"state": "busy", "progress_this_turn": 2}),
]


@pytest.mark.parametrize("start,event,payload,expected", ROWS,
                         ids=[f"{r[0]['state']}-{r[1]}-{i}" for i, r in enumerate(ROWS)])
def test_table(start, event, payload, expected) -> None:
    before = dict(start)
    new = S.apply(start, event, payload, T1)
    assert start == before, "apply must not mutate its input"
    for k, v in expected.items():
        assert new[k] == v, (k, new[k], v)
    assert new["last_event"] == event
    assert new["state"] in S.STATES
    if new["state"] != start["state"] or new["reason"] != start["reason"]:
        assert new["since"] == T1
    else:
        assert new["since"] == T0


@pytest.mark.parametrize("start,event,payload,expected",
                         [r for r in ROWS if r[1] != "progress"])
def test_idempotent(start, event, payload, expected) -> None:
    once = S.apply(start, event, payload, T1)
    assert S.apply(once, event, payload, T1) == once
    # a later replay does not move `since` either
    assert S.apply(once, event, payload, "2026-10-05T11:00:00+09:00") == once


def test_progress_is_additive() -> None:
    s = S.apply(st("busy"), "progress", {}, T1)
    s = S.apply(s, "progress", {}, T1)
    assert s["progress_this_turn"] == 2


@pytest.mark.parametrize("state", sorted(S.STATES))
def test_pane_dead_from_every_state(state: str) -> None:
    new = S.apply(st(state), "pane_dead", {}, T1)
    assert new["state"] == "down" and new["reason"] == "pane_dead"


def test_unknown_event() -> None:
    with pytest.raises(ValueError):
        S.apply(st("idle"), "bogus", {}, T1)


def test_is_deliverable() -> None:
    assert S.is_deliverable(st("idle"))
    for s in S.STATES - {"idle"}:
        assert not S.is_deliverable(st(s))
    assert not S.is_deliverable(None)


def test_alert_needed() -> None:
    for e in S.UNRECOVERABLE_ERRORS:
        assert S.alert_needed({"error_type": e})
        assert S.alert_needed({"error": e})
    assert not S.alert_needed({"error_type": "rate_limit"})
    assert not S.alert_needed({})


def test_default_and_missing_fields() -> None:
    new = S.apply(None, "session-start", {"source": "startup"}, T1)
    assert set(S.default_state()) <= set(new)
    assert new["state"] == "idle"
    assert not S.is_deliverable(S.default_state())


def test_transition_file(tmp_path: Path) -> None:
    rt = ProjectState.of(tmp_path, "demo")
    assert S.read_state(rt, "planner")["state"] == "down"
    old, new = S.transition(rt, "planner", "respawn")
    assert old["state"] == "down" and new["state"] == "starting"
    S.transition(rt, "planner", "session-start", {"source": "startup", "session_id": "abc"})
    S.transition(rt, "planner", "prompt-submit", {})
    S.transition(rt, "planner", "progress")
    got = S.read_state(rt, "planner")
    assert got["state"] == "busy" and got["progress_this_turn"] == 1 and got["session_id"] == "abc"
    assert rt.state_file("planner").exists()
    states = S.all_states(rt)
    assert list(states)[0] == "orchestrator" and states["planner"]["state"] == "busy"
    with pytest.raises(ValueError):
        S.transition(rt, "planner", "nope")
    assert S.read_state(rt, "planner")["state"] == "busy"
