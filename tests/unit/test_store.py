"""Message store, global seq, status transitions and bus.jsonl (M1a)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ads.bus import store
from ads.bus.log import log_event, read_events
from ads.paths import Runtime


@pytest.fixture
def rt(tmp_runtime: Path) -> Runtime:
    return Runtime(tmp_runtime)


def _mk(rt: Runtime, to: str = "planner", **kw):
    kw.setdefault("from_", "orchestrator")
    kw.setdefault("type", "instruct")
    kw.setdefault("body", "body")
    return store.create(rt, to=to, **kw)


def test_seq_monotonic_under_threads(rt: Runtime) -> None:
    results: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        local = [store.next_seq(rt) for _ in range(50)]
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 400
    assert sorted(results) == list(range(1, 401))
    assert int(rt.seq.read_text()) == 400


def test_seq_reseeds_from_messages_if_counter_lost(rt: Runtime) -> None:
    for _ in range(3):
        _mk(rt)
    rt.seq.unlink()
    assert store.next_seq(rt) == 4


def test_create_writes_both_files(rt: Runtime) -> None:
    m = _mk(rt, subject="Plan it", body="hello\nworld")
    assert m.id.startswith("m-") and m.seq == 1
    assert (rt.msgs / f"{m.id}.md").read_text() == "hello\nworld\n"
    data = json.loads((rt.msgs / f"{m.id}.json").read_text())
    assert data["from"] == "orchestrator" and data["to"] == "planner"
    assert data["status"] == "queued" and data["expects_reply"] is True
    assert store.get(rt, m.id) == m
    assert store.body_path(rt, m.id) == rt.msgs / f"{m.id}.md"
    info = _mk(rt, type="info")
    assert info.expects_reply is False
    assert [e["event"] for e in read_events(rt)] == ["created", "created"]


def test_create_rejects_bad_input(rt: Runtime) -> None:
    with pytest.raises(ValueError):
        _mk(rt, type="bogus")
    with pytest.raises(ValueError):
        _mk(rt, type="report", result="pass")
    with pytest.raises(store.TransitionError):
        _mk(rt, status="delivering")


def test_get_missing(rt: Runtime) -> None:
    with pytest.raises(store.MessageNotFound):
        store.get(rt, "m-20261005-000099")
    with pytest.raises(store.MessageNotFound):
        store.update(rt, "m-20261005-000099", enters=1)


def test_queued_for_orders_by_seq_not_id(rt: Runtime) -> None:
    # seq 1 created "tomorrow", seq 2 "today": ids sort the other way round.
    today = datetime(2026, 10, 5, 12, 0).astimezone()
    a = _mk(rt, now=today + timedelta(days=1))
    b = _mk(rt, now=today)
    c = _mk(rt, now=today - timedelta(days=400))
    assert sorted([a.id, b.id, c.id]) != [a.id, b.id, c.id]
    _mk(rt, to="tester")
    assert [m.id for m in store.queued_for(rt, "planner")] == [a.id, b.id, c.id]
    store.update(rt, a.id, status="held", held_by="m-20261005-000009")
    store.update(rt, c.id, status="held")
    assert [m.id for m in store.queued_for(rt, "planner")] == [b.id]
    assert [m.id for m in store.held_all(rt)] == [a.id, c.id]
    store.update(rt, b.id, status="delivering")
    assert [m.id for m in store.inflight_for(rt, "planner")] == [b.id]
    assert store.inflight_for(rt, "tester") == []
    assert [m.seq for m in store.all_messages(rt)] == [1, 2, 3, 4]


LEGAL = [
    ("queued", "held"), ("queued", "delivering"), ("queued", "superseded"),
    ("queued", "failed"), ("queued", "ignored"),
    ("held", "queued"), ("held", "superseded"), ("held", "failed"), ("held", "ignored"),
    ("delivering", "delivered"), ("delivering", "queued"), ("delivering", "failed"),
    ("failed", "queued"),
]
ALL = ["queued", "held", "delivering", "delivered", "failed", "superseded", "ignored"]
ILLEGAL = [(a, b) for a in ALL for b in ALL if a != b and (a, b) not in LEGAL]

# How to reach each starting status from a fresh queued message.
PATH = {
    "queued": [], "held": ["held"], "delivering": ["delivering"],
    "delivered": ["delivering", "delivered"], "failed": ["failed"],
    "superseded": ["superseded"], "ignored": ["ignored"],
}


def _at(rt: Runtime, status: str):
    m = _mk(rt)
    for s in PATH[status]:
        m = store.update(rt, m.id, status=s)
    assert m.status == status
    return m


@pytest.mark.parametrize(("old", "new"), LEGAL)
def test_legal_transitions(rt: Runtime, old: str, new: str) -> None:
    m = _at(rt, old)
    m2 = store.update(rt, m.id, status=new)
    assert m2.status == new == store.get(rt, m.id).status


@pytest.mark.parametrize(("old", "new"), ILLEGAL)
def test_illegal_transitions(rt: Runtime, old: str, new: str) -> None:
    m = _at(rt, old)
    before = (rt.msgs / f"{m.id}.json").read_text()
    with pytest.raises(store.TransitionError):
        store.update(rt, m.id, status=new)
    assert (rt.msgs / f"{m.id}.json").read_text() == before


def test_transition_table_consistent() -> None:
    assert set(store.TRANSITIONS) == set(ALL)
    assert {(a, b) for a, nxt in store.TRANSITIONS.items() for b in nxt} == set(LEGAL)
    assert store.TERMINAL == {"delivered", "superseded", "ignored"}


def test_same_status_and_field_updates(rt: Runtime) -> None:
    m = _mk(rt)
    store.update(rt, m.id, status="queued", pastes=2)
    assert store.get(rt, m.id).pastes == 2
    with pytest.raises(ValueError):
        store.update(rt, m.id, seq=5)
    with pytest.raises(ValueError):
        store.update(rt, m.id, nonsense=1)
    with pytest.raises(ValueError):
        store.update(rt, m.id, type="report", result="bogus")


def test_delivered_stamps_time_and_unconfirmed(rt: Runtime) -> None:
    m = _mk(rt)
    store.update(rt, m.id, status="delivering")
    m2 = store.update(rt, m.id, status="delivered", unconfirmed=True)
    assert m2.delivered_at and m2.unconfirmed is True
    h = _mk(rt, to="human", type="report", re=m.id, result="success", status="delivered")
    assert h.delivered_at is not None


def test_bus_jsonl_one_line_per_transition(rt: Runtime) -> None:
    m = _mk(rt)
    store.update(rt, m.id, pastes=1)                 # no transition: no line
    store.update(rt, m.id, status="delivering")
    store.update(rt, m.id, enters=1)                 # no line
    store.update(rt, m.id, status="delivered")
    with pytest.raises(store.TransitionError):
        store.update(rt, m.id, status="queued")      # rejected: no line
    lines = (rt.logs / "bus.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in lines]
    assert [e["event"] for e in events] == ["created", "status:delivering", "status:delivered"]
    last = events[-1]
    assert last["id"] == m.id and last["from"] == "orchestrator" and last["to"] == "planner"
    assert last["type"] == "instruct" and last["status"] == "delivered"
    assert last["extra"] == {"prev": "delivering"}
    assert set(last) == {"ts", "event", "id", "from", "to", "type", "status", "extra"}


def test_log_event_concurrent_lines_intact(rt: Runtime) -> None:
    big = "z" * 5000

    def worker(i: int) -> None:
        for j in range(50):
            log_event(rt, "test", None, i=i, j=j, pad=big)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (rt.logs / "bus.jsonl").read_text().splitlines()
    assert len(lines) == 400
    assert all(json.loads(line)["extra"]["pad"] == big for line in lines)
    assert len(read_events(rt)) == 400


def test_update_atomic_under_concurrency(rt: Runtime) -> None:
    m = _mk(rt)
    path = rt.msgs / f"{m.id}.json"
    stop = threading.Event()
    errors: list[Exception] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                json.loads(path.read_text())
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    def writer() -> None:
        for _ in range(25):
            store.bump(rt, m.id, "enters")  # read-modify-write must not lose increments

    def updater() -> None:
        for _ in range(25):
            cur = store.get(rt, m.id)
            store.update(rt, m.id, subject=f"s{cur.enters}")

    r = threading.Thread(target=reader)
    r.start()
    ws = [threading.Thread(target=writer) for _ in range(6)] + \
         [threading.Thread(target=updater) for _ in range(2)]
    for t in ws:
        t.start()
    for t in ws:
        t.join()
    stop.set()
    r.join()
    assert errors == []
    assert store.get(rt, m.id).enters == 150
    with pytest.raises(ValueError):
        store.bump(rt, m.id, "seq")
