"""Task ledger (M1c, plan §4.6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ads.bus import ledger as L
from ads.bus import state as S
from ads.bus import store
from ads.config import default_config
from ads.paths import ProjectState


@pytest.fixture
def rt(tmp_path: Path) -> ProjectState:
    r = ProjectState.of(tmp_path / "runtime", "demo")
    r.ensure()
    return r


@pytest.fixture
def cfg():
    return default_config()


def send(rt, cfg, frm, to, typ, **kw):
    kw.setdefault("subject", f"{typ} {frm}->{to}")
    kw.setdefault("body", "body")
    return L.send(rt, cfg, from_=frm, to=to, type=typ, **kw)


def deliver(rt, msg_id):
    """Simulate the supervisor paste + prompt-submit confirmation."""
    store.update(rt, msg_id, status="delivering")
    L.mark_delivered(rt, msg_id)


def status(rt, mid):
    return store.get(rt, mid).status


def tstate(rt, tid):
    return L.get_task(rt, tid)["state"]


# --- basics -----------------------------------------------------------------------------

def test_task_created_and_fields(rt, cfg) -> None:
    m = send(rt, cfg, "human", "orchestrator", "instruct")
    t = L.get_task(rt, m.id)
    assert t["id"] == m.id and t["seq"] == m.seq and t["state"] == "queued"
    for k in ("from", "to", "type", "parent", "reply_id", "nudges", "created", "closed_at"):
        assert k in t
    assert m.status == "queued" and m.expects_reply
    assert rt.poke.exists()
    i = send(rt, cfg, "planner", "orchestrator", "info")
    assert L.get_task(rt, i.id) is None


def test_to_human_delivered_immediately(rt, cfg) -> None:
    q = send(rt, cfg, "orchestrator", "human", "question")
    assert q.status == "delivered"
    assert tstate(rt, q.id) == "delivered"
    a = send(rt, cfg, "human", "orchestrator", "answer", re=q.id)
    assert tstate(rt, q.id) == "closed" and L.get_task(rt, q.id)["reply_id"] == a.id


def test_mark_delivered_idempotent(rt, cfg) -> None:
    m = send(rt, cfg, "orchestrator", "planner", "instruct")
    deliver(rt, m.id)
    assert status(rt, m.id) == "delivered" and tstate(rt, m.id) == "delivered"
    L.mark_delivered(rt, m.id)
    assert status(rt, m.id) == "delivered" and tstate(rt, m.id) == "delivered"
    # queued straight to delivered (e.g. after supervisor recovery) also works
    i = send(rt, cfg, "orchestrator", "tester", "info")
    L.mark_delivered(rt, i.id)
    assert status(rt, i.id) == "delivered"
    assert L.mark_delivered(rt, "m-20991231-999999") is None


# --- hold rules -------------------------------------------------------------------------

def test_hold_rule1_same_pair(rt, cfg) -> None:
    a = send(rt, cfg, "developer", "coder-1", "instruct")
    b = send(rt, cfg, "developer", "coder-1", "instruct")
    c = send(rt, cfg, "developer", "coder-2", "instruct")  # other pair: not held
    assert a.status == "queued" and c.status == "queued"
    assert b.status == "held" and b.held_by == a.id
    deliver(rt, a.id)
    r = send(rt, cfg, "coder-1", "developer", "report", re=a.id, result="success")
    assert r.status == "queued"
    assert status(rt, b.id) == "queued" and store.get(rt, b.id).held_by is None


def test_hold_rule2_orchestrator_phase(rt, cfg) -> None:
    a = send(rt, cfg, "orchestrator", "planner", "instruct")
    b = send(rt, cfg, "orchestrator", "developer", "instruct")
    assert b.status == "held" and b.held_by == a.id
    # orchestrator questions are not held; review-requests from orchestrator are
    q = send(rt, cfg, "orchestrator", "human", "question")
    assert q.status == "delivered"
    rr = send(rt, cfg, "orchestrator", "evaluator", "review-request")
    assert rr.status == "held"
    # non-orchestrator senders are not phase-serialized
    d = send(rt, cfg, "developer", "coder-1", "instruct")
    assert d.status == "queued"


def test_review_request_held_behind_review_request(rt, cfg) -> None:
    a = send(rt, cfg, "planner", "evaluator", "review-request")
    b = send(rt, cfg, "planner", "evaluator", "review-request")
    assert b.status == "held"
    deliver(rt, a.id)
    send(rt, cfg, "evaluator", "planner", "review", re=a.id, result="revise")
    assert status(rt, b.id) == "queued"


def test_human_exempt(rt, cfg) -> None:
    a = send(rt, cfg, "human", "orchestrator", "instruct")
    b = send(rt, cfg, "human", "orchestrator", "instruct")
    assert a.status == b.status == "queued"


def test_supersede_bypasses_hold(rt, cfg) -> None:
    a = send(rt, cfg, "orchestrator", "planner", "instruct")
    deliver(rt, a.id)
    b = send(rt, cfg, "orchestrator", "planner", "instruct", subject="Cancel", supersede=a.id)
    assert b.status == "queued" and b.supersedes == a.id
    assert tstate(rt, a.id) == "superseded"
    assert status(rt, a.id) == "delivered"  # already delivered: not touched


def test_hold_reason_excludes_own_task(rt, cfg) -> None:
    a = send(rt, cfg, "developer", "coder-1", "instruct")
    b = send(rt, cfg, "developer", "coder-1", "instruct")
    assert L.hold_reason(rt, store.get(rt, b.id)) == a.id
    L.supersede_task(rt, a.id)  # direct API; a's task gone
    # b's own task (same pair, open) must not hold b
    assert L.hold_reason(rt, store.get(rt, b.id)) is None


def test_hold_reason_none_for_non_holdable(rt, cfg) -> None:
    a = send(rt, cfg, "developer", "coder-1", "instruct")
    i = send(rt, cfg, "developer", "coder-1", "info")
    q = send(rt, cfg, "developer", "coder-1", "question")
    assert i.status == "queued" and q.status == "queued"
    assert L.hold_reason(rt, store.get(rt, i.id)) is None
    assert a.status == "queued"


def test_release_oldest_then_recheck(rt, cfg) -> None:
    a = send(rt, cfg, "orchestrator", "planner", "instruct")
    b = send(rt, cfg, "orchestrator", "developer", "instruct")
    c = send(rt, cfg, "orchestrator", "tester", "instruct")
    assert b.status == "held" and c.status == "held"
    deliver(rt, a.id)
    send(rt, cfg, "planner", "orchestrator", "report", re=a.id, result="success")
    assert status(rt, b.id) == "queued"
    c2 = store.get(rt, c.id)
    assert c2.status == "held" and c2.held_by == b.id
    deliver(rt, b.id)
    send(rt, cfg, "developer", "orchestrator", "report", re=b.id, result="partial")
    assert status(rt, c.id) == "queued"
    assert L.release_held(rt) == []


def test_release_held_returns_ids(rt, cfg) -> None:
    a = send(rt, cfg, "developer", "coder-1", "instruct")
    b = send(rt, cfg, "developer", "coder-1", "instruct")
    L._update_task(rt, a.id, state="closed")
    assert L.release_held(rt) == [b.id]


# --- supersede --------------------------------------------------------------------------

def test_supersede_cascade_grandchildren(rt, cfg) -> None:
    t0 = send(rt, cfg, "human", "orchestrator", "instruct")
    deliver(rt, t0.id)
    t1 = send(rt, cfg, "orchestrator", "developer", "instruct", parent=t0.id)
    deliver(rt, t1.id)
    t2 = send(rt, cfg, "developer", "coder-1", "instruct", parent=t1.id)
    deliver(rt, t2.id)
    t3 = send(rt, cfg, "developer", "coder-2", "instruct", parent=t1.id)  # undelivered child
    t4 = send(rt, cfg, "coder-1", "coder-2", "question", parent=t2.id)  # grandchild, undelivered
    t2b = send(rt, cfg, "developer", "coder-1", "instruct", parent=t1.id)  # held child
    assert t2b.status == "held"
    other = send(rt, cfg, "developer", "coder-2", "info")
    new = send(rt, cfg, "orchestrator", "developer", "instruct", subject="Cancel", supersede=t1.id)
    assert new.status == "queued"
    for tid in (t1.id, t2.id, t3.id, t4.id, t2b.id):
        assert tstate(rt, tid) == "superseded", tid
    assert tstate(rt, t0.id) == "delivered"
    assert status(rt, t2.id) == "delivered"
    for mid in (t3.id, t4.id, t2b.id):
        assert status(rt, mid) == "superseded", mid
    assert status(rt, other.id) == "queued"


def test_late_reply_ignored(rt, cfg) -> None:
    a = send(rt, cfg, "orchestrator", "planner", "instruct")
    deliver(rt, a.id)
    send(rt, cfg, "orchestrator", "planner", "instruct", subject="Cancel", supersede=a.id)
    late = send(rt, cfg, "planner", "orchestrator", "report", re=a.id, result="success")
    assert late.status == "ignored"
    assert tstate(rt, a.id) == "superseded" and L.get_task(rt, a.id)["reply_id"] is None
    # late reply to a failed task
    b = send(rt, cfg, "developer", "coder-1", "instruct")
    deliver(rt, b.id)
    L.agent_down_cascade(rt, "coder-1")
    late2 = send(rt, cfg, "coder-1", "developer", "report", re=b.id, result="failure")
    assert late2.status == "ignored"


# --- validation -------------------------------------------------------------------------

def test_reply_validation_errors(rt, cfg) -> None:
    a = send(rt, cfg, "orchestrator", "planner", "instruct")
    q = send(rt, cfg, "planner", "orchestrator", "question")
    cases = [
        dict(frm="planner", to="nobody", typ="info"),
        dict(frm="ghost", to="planner", typ="info"),
        dict(frm="planner", to="planner", typ="info"),
        dict(frm="planner", to="orchestrator", typ="report", result="success"),  # no --re
        dict(frm="planner", to="orchestrator", typ="report", re="m-20991231-000001",
             result="success"),
        dict(frm="tester", to="orchestrator", typ="report", re=a.id, result="success"),  # not to me
        dict(frm="planner", to="orchestrator", typ="review", re=a.id, result="pass"),  # wrong type
        dict(frm="planner", to="tester", typ="report", re=a.id, result="success"),  # wrong to
        dict(frm="planner", to="orchestrator", typ="report", re=a.id, result="pass"),  # bad result
        dict(frm="planner", to="orchestrator", typ="report", re=a.id),  # missing result
        dict(frm="orchestrator", to="planner", typ="answer", re=a.id),  # a not addressed to orch
        dict(frm="planner", to="orchestrator", typ="info", supersede=q.id),  # not task type
        dict(frm="developer", to="planner", typ="instruct", supersede=a.id),  # not own task
        dict(frm="orchestrator", to="planner", typ="instruct", supersede="m-20991231-000001"),
        dict(frm="orchestrator", to="planner", typ="instruct", parent="m-20991231-000001"),
        dict(frm="orchestrator", to="planner", typ="instruct", re=a.id),
        dict(frm="orchestrator", to="planner", typ="system", result="agent-down"),
        dict(frm="orchestrator", to="planner", typ="bogus"),
    ]
    for c in cases:
        frm, to, typ = c.pop("frm"), c.pop("to"), c.pop("typ")
        with pytest.raises(L.LedgerError):
            send(rt, cfg, frm, to, typ, **c)
    # duplicate reply to a closed task
    send(rt, cfg, "orchestrator", "planner", "answer", re=q.id)
    with pytest.raises(L.LedgerError):
        send(rt, cfg, "orchestrator", "planner", "answer", re=q.id)
    # supersede of a non-open task
    with pytest.raises(L.LedgerError):
        send(rt, cfg, "planner", "orchestrator", "question", supersede=q.id)


# --- down cascade -----------------------------------------------------------------------

def test_down_cascade(rt, cfg) -> None:
    a = send(rt, cfg, "developer", "coder-1", "instruct")
    deliver(rt, a.id)
    b = send(rt, cfg, "developer", "coder-1", "instruct")  # held behind a
    h = send(rt, cfg, "human", "orchestrator", "instruct")
    deliver(rt, h.id)
    q = send(rt, cfg, "orchestrator", "human", "question")  # orchestrator -> human, open
    assert b.status == "held"
    ids = L.agent_down_cascade(rt, "coder-1")
    assert tstate(rt, a.id) == "failed"
    # b (held, open incoming) fails too; its message goes held -> failed
    assert tstate(rt, b.id) == "failed" and status(rt, b.id) == "failed"
    assert len(ids) == 2
    sysmsgs = [store.get(rt, i) for i in ids]
    assert all(m.type == "system" and m.result == "agent-down" and m.to == "developer"
               for m in sysmsgs)
    assert {m.re for m in sysmsgs} == {a.id, b.id}
    # a down orchestrator notifies the human (delivered immediately)
    ids2 = L.agent_down_cascade(rt, "orchestrator")
    assert len(ids2) == 1 and store.get(rt, ids2[0]).status == "delivered"
    assert tstate(rt, q.id) == "delivered"  # outgoing tasks untouched


def test_down_cascade_releases_held(rt, cfg) -> None:
    a = send(rt, cfg, "orchestrator", "planner", "instruct")
    deliver(rt, a.id)
    b = send(rt, cfg, "orchestrator", "developer", "instruct")
    assert b.status == "held"
    L.agent_down_cascade(rt, "planner")
    assert status(rt, b.id) == "queued"


# --- stop decision ----------------------------------------------------------------------

def _busy(rt, agent):
    S.transition(rt, agent, "session-start", {"source": "startup"})
    S.transition(rt, agent, "prompt-submit", {})


def test_stop_no_tasks(rt, cfg) -> None:
    _busy(rt, "planner")
    assert L.stop_decision(rt, cfg, "planner") is None


def test_stop_queued_task_not_counted(rt, cfg) -> None:
    send(rt, cfg, "orchestrator", "planner", "instruct")
    _busy(rt, "planner")
    assert L.stop_decision(rt, cfg, "planner") is None


def test_stop_info_does_not_count(rt, cfg) -> None:
    a = send(rt, cfg, "orchestrator", "planner", "instruct", subject='Plan "x"')
    deliver(rt, a.id)
    _busy(rt, "planner")
    send(rt, cfg, "planner", "orchestrator", "info")
    reason = L.stop_decision(rt, cfg, "planner")
    assert reason is not None
    from ads.launcher import ads_bin
    assert (f"{ads_bin(rt)} send --to orchestrator --type report --re {a.id} "
            f"--result success|partial|failure") in reason
    assert "--body-file " in reason and "--subject \"Re: Plan 'x'\"" in reason
    assert L.get_task(rt, a.id)["nudges"] == 1


def test_stop_reply_counts(rt, cfg) -> None:
    a = send(rt, cfg, "orchestrator", "planner", "instruct")
    b = send(rt, cfg, "tester", "planner", "question")
    deliver(rt, a.id)
    deliver(rt, b.id)
    _busy(rt, "planner")
    send(rt, cfg, "planner", "tester", "answer", re=b.id)
    assert S.read_state(rt, "planner")["progress_this_turn"] == 1
    assert L.stop_decision(rt, cfg, "planner") is None


def test_stop_task_creating_send_counts(rt, cfg) -> None:
    a = send(rt, cfg, "orchestrator", "developer", "instruct")
    deliver(rt, a.id)
    _busy(rt, "developer")
    send(rt, cfg, "developer", "coder-1", "instruct", parent=a.id)
    assert L.stop_decision(rt, cfg, "developer") is None
    # next turn: progress reset, but the open outgoing task suppresses the nudge
    S.transition(rt, "developer", "prompt-submit", {})
    assert L.stop_decision(rt, cfg, "developer") is None


def test_stop_open_outgoing_suppresses(rt, cfg) -> None:
    a = send(rt, cfg, "orchestrator", "planner", "instruct")
    deliver(rt, a.id)
    send(rt, cfg, "planner", "evaluator", "review-request", parent=a.id)
    _busy(rt, "planner")  # progress reset by prompt-submit
    assert L.stop_decision(rt, cfg, "planner") is None


def test_stop_multiple_tasks_reply_types(rt, cfg) -> None:
    a = send(rt, cfg, "orchestrator", "planner", "instruct")
    b = send(rt, cfg, "tester", "planner", "question")
    c = send(rt, cfg, "developer", "planner", "review-request")
    for m in (a, b, c):
        deliver(rt, m.id)
    _busy(rt, "planner")
    reason = L.stop_decision(rt, cfg, "planner")
    assert f"--to orchestrator --type report --re {a.id} --result success|partial|failure" in reason
    assert f"--to tester --type answer --re {b.id} --subject" in reason
    assert f"--to developer --type review --re {c.id} --result pass|revise" in reason
    assert all(L.get_task(rt, m.id)["nudges"] == 1 for m in (a, b, c))


def test_nudge_cap_then_missing_report(rt, cfg) -> None:
    a = send(rt, cfg, "orchestrator", "planner", "instruct")
    deliver(rt, a.id)
    _busy(rt, "planner")
    assert L.exhausted_tasks(rt, cfg) == []
    for _ in range(cfg.protocol.max_report_nudges):
        assert L.stop_decision(rt, cfg, "planner") is not None
    assert L.stop_decision(rt, cfg, "planner") is None
    ex = L.exhausted_tasks(rt, cfg)
    assert [t["id"] for t in ex] == [a.id]
    sid = L.fail_missing_report(rt, ex[0])
    assert tstate(rt, a.id) == "failed"
    m = store.get(rt, sid)
    assert m.type == "system" and m.result == "missing-report" and m.to == "orchestrator"
    assert m.re == a.id
    assert L.fail_missing_report(rt, ex[0]) is None  # idempotent
    assert L.exhausted_tasks(rt, cfg) == []


def test_api_error_notify(rt, cfg) -> None:
    a = send(rt, cfg, "orchestrator", "planner", "instruct")
    deliver(rt, a.id)
    ids = L.api_error_notify(rt, "planner", "billing_error")
    m = store.get(rt, ids[0])
    assert m.result == "api-error" and m.to == "orchestrator" and m.re == a.id
    assert "billing_error" in store.read_body(rt, m.id)


def test_progress_not_bumped_for_info_or_human(rt, cfg) -> None:
    _busy(rt, "planner")
    send(rt, cfg, "planner", "orchestrator", "info")
    assert S.read_state(rt, "planner")["progress_this_turn"] == 0
    send(rt, cfg, "planner", "evaluator", "review-request")
    assert S.read_state(rt, "planner")["progress_this_turn"] == 1
    send(rt, cfg, "human", "orchestrator", "instruct")
    assert not rt.state_file("human").exists()


# --- phase ------------------------------------------------------------------------------

def test_current_phase(rt, cfg) -> None:
    assert L.current_phase(rt) is None
    a = send(rt, cfg, "orchestrator", "planner", "instruct")
    assert L.current_phase(rt) == "plan"
    b = send(rt, cfg, "orchestrator", "developer", "instruct")  # held: not current
    assert L.current_phase(rt) == "plan"
    deliver(rt, a.id)
    send(rt, cfg, "planner", "orchestrator", "report", re=a.id, result="success")
    assert L.current_phase(rt) == "dev"
    deliver(rt, b.id)
    send(rt, cfg, "developer", "orchestrator", "report", re=b.id, result="success")
    send(rt, cfg, "orchestrator", "tester", "instruct")
    assert L.current_phase(rt) == "test"


# --- M5 follow-ups ----------------------------------------------------------------------

def _systems(rt, result):
    return [m for m in store.all_messages(rt) if m.type == "system" and m.result == result]


def test_supersede_notifies_delivered_children(rt, cfg) -> None:
    """F1: a cascaded child whose message was already delivered gets system(superseded)."""
    t0 = send(rt, cfg, "human", "orchestrator", "instruct")
    deliver(rt, t0.id)
    t1 = send(rt, cfg, "orchestrator", "developer", "instruct", parent=t0.id)
    deliver(rt, t1.id)
    t2 = send(rt, cfg, "developer", "coder-1", "instruct", parent=t1.id)  # delivered child
    deliver(rt, t2.id)
    t3 = send(rt, cfg, "developer", "coder-2", "instruct", parent=t1.id)  # undelivered child
    t4 = send(rt, cfg, "coder-1", "coder-2", "question", parent=t2.id)    # delivering grandchild
    store.update(rt, t4.id, status="delivering")
    t5 = send(rt, cfg, "developer", "human", "question", parent=t1.id)    # human: no system msg
    send(rt, cfg, "orchestrator", "developer", "instruct", subject="Cancel", supersede=t1.id)
    sys_msgs = _systems(rt, "superseded")
    assert sorted((m.to, m.re) for m in sys_msgs) == sorted([("coder-1", t2.id),
                                                             ("coder-2", t4.id)])
    for m in sys_msgs:
        assert m.from_ == L.SYSTEM_SENDER and m.status == "queued"
        assert m.subject == f"Task {m.re} was superseded"
        assert "stop that work and do not report on it" in store.read_body(rt, m.id)
    # the root's assignee (developer) is told by the SUPERSEDES pointer, not a system msg
    assert all(m.re != t1.id for m in sys_msgs)
    assert tstate(rt, t3.id) == tstate(rt, t5.id) == "superseded"


def test_supersede_direct_root_only_no_notice(rt, cfg) -> None:
    a = send(rt, cfg, "orchestrator", "planner", "instruct")
    deliver(rt, a.id)
    send(rt, cfg, "orchestrator", "planner", "instruct", subject="Cancel", supersede=a.id)
    assert _systems(rt, "superseded") == []


def test_stop_held_outgoing_counts_as_waiting(rt, cfg) -> None:
    """F2: a held instruct/review-request the agent sent counts as open outgoing → no nudge."""
    b = send(rt, cfg, "orchestrator", "tester", "instruct")
    deliver(rt, b.id)
    c = send(rt, cfg, "tester", "developer", "instruct", parent=b.id)
    deliver(rt, c.id)
    h = send(rt, cfg, "tester", "developer", "instruct", parent=b.id)
    assert h.status == "held" and h.held_by == c.id
    L._update_task(rt, c.id, state="closed")  # out of band: only the still-held h is open
    _busy(rt, "tester")                        # progress_this_turn reset
    assert store.get(rt, h.id).status == "held"
    assert [t["id"] for t in L.open_outgoing(rt, "tester")] == [h.id]
    assert L.stop_decision(rt, cfg, "tester") is None
    assert L.get_task(rt, b.id)["nudges"] == 0


def test_reply_command_uses_absolute_ads_bin(rt, cfg) -> None:
    """F3: the Stop-hook nudge names the absolute ads binary (launcher resolver)."""
    from ads.launcher import ads_bin
    a = send(rt, cfg, "orchestrator", "planner", "instruct")
    deliver(rt, a.id)
    _busy(rt, "planner")
    reason = L.stop_decision(rt, cfg, "planner")
    bin_ = ads_bin(rt)
    assert bin_.is_absolute()
    assert f"- {a.id} (instruct from orchestrator): {bin_} send --to orchestrator" in reason


def test_info_with_re_rejected(rt, cfg) -> None:
    """F4: --re is only for replies."""
    a = send(rt, cfg, "orchestrator", "planner", "instruct")
    for typ in ("info",):
        with pytest.raises(L.LedgerError, match="--re is only for replies"):
            send(rt, cfg, "planner", "orchestrator", typ, re=a.id)
    with pytest.raises(L.LedgerError, match="--re is only for replies"):
        send(rt, cfg, "planner", "orchestrator", "info", re="m-20260101-999999")
