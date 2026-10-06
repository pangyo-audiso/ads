"""Task ledger: `work/tasks/<id>.json`, hold/release, supersede, cascades, stop decision (§4.6).

A task is created for every task-creating message (instruct, review-request, question); its
id is the creating message's id. Task states: queued | delivered | closed | superseded |
failed. Open = queued | delivered.

All mutating entry points run under one re-entrant ledger lock (`work/run/ledger.lock`)
so hook processes, `ads send` and the supervisor never interleave a read-check-write.
Stdlib only (hooks import this).
"""

from __future__ import annotations

import fcntl
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from ads.bus import state as agent_state
from ads.bus import store
from ads.bus.envelope import MSG_TYPES, REPLY_TYPES, TASK_TYPES, Message, validate_result
from ads.bus.log import log_event
from ads.paths import AGENTS, HUMAN, Runtime, locked_json

TASK_STATES: frozenset[str] = frozenset({"queued", "delivered", "closed", "superseded", "failed"})
OPEN: frozenset[str] = frozenset({"queued", "delivered"})
HOLDABLE: frozenset[str] = frozenset({"instruct", "review-request"})
UNDELIVERED: frozenset[str] = frozenset({"queued", "held"})

# Sender name used for messages ads synthesizes itself (type=system).
SYSTEM_SENDER = "ads"

PHASES: dict[str, str] = {"planner": "plan", "developer": "dev", "tester": "test"}

# task type -> reply type (inverse of REPLY_TYPES)
REPLY_FOR: dict[str, str] = {v: k for k, v in REPLY_TYPES.items()}
RESULT_HINT: dict[str, str] = {"report": "success|partial|failure", "review": "pass|revise"}
RESULT_REQUIRED: frozenset[str] = frozenset({"report", "review"})


class LedgerError(ValueError):
    """`ads send` validation failure (message is not created)."""


def _rt(runtime: Runtime | Path | str) -> Runtime:
    return runtime if isinstance(runtime, Runtime) else Runtime(Path(runtime))


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --- lock -------------------------------------------------------------------------------

_local = threading.local()


@contextmanager
def ledger_lock(runtime: Runtime | Path | str) -> Iterator[None]:
    """Exclusive flock on `work/run/ledger.lock`; re-entrant within a thread."""
    rt = _rt(runtime)
    key = str(rt.root.resolve())
    depth: dict[str, int] = _local.__dict__.setdefault("depth", {})
    if depth.get(key):
        depth[key] += 1
        try:
            yield
        finally:
            depth[key] -= 1
        return
    rt.run.mkdir(parents=True, exist_ok=True)
    with open(rt.run / "ledger.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        depth[key] = 1
        try:
            yield
        finally:
            depth[key] = 0
            fcntl.flock(fh, fcntl.LOCK_UN)


# --- task store -------------------------------------------------------------------------

def task_path(runtime: Runtime | Path | str, task_id: str) -> Path:
    return _rt(runtime).tasks / f"{task_id}.json"


def get_task(runtime: Runtime | Path | str, task_id: str) -> dict[str, Any] | None:
    try:
        data = json.loads(task_path(runtime, task_id).read_text())
    except (FileNotFoundError, ValueError):
        return None
    return data if isinstance(data, dict) and data else None


def all_tasks(runtime: Runtime | Path | str) -> list[dict[str, Any]]:
    """Every task, sorted by seq."""
    out = []
    for p in _rt(runtime).tasks.glob("m-*.json"):
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("id"):
            out.append(d)
    out.sort(key=lambda t: t.get("seq", 0))
    return out


def open_tasks(runtime: Runtime | Path | str) -> list[dict[str, Any]]:
    return [t for t in all_tasks(runtime) if t.get("state") in OPEN]


def open_incoming(runtime: Runtime | Path | str, agent: str) -> list[dict[str, Any]]:
    """Open tasks addressed to `agent` (by seq)."""
    return [t for t in open_tasks(runtime) if t["to"] == agent]


def open_outgoing(runtime: Runtime | Path | str, agent: str) -> list[dict[str, Any]]:
    """Open tasks `agent` created (by seq)."""
    return [t for t in open_tasks(runtime) if t["from"] == agent]


def _create_task(rt: Runtime, msg: Message) -> dict[str, Any]:
    task = {
        "id": msg.id, "seq": msg.seq, "from": msg.from_, "to": msg.to, "type": msg.type,
        "subject": msg.subject,
        "state": "delivered" if msg.status == "delivered" else "queued",
        "parent": msg.parent, "reply_id": None, "nudges": 0,
        "created": msg.created, "closed_at": None,
    }
    with locked_json(task_path(rt, msg.id)) as data:
        data.clear()
        data.update(task)
    log_event(rt, f"task:{task['state']}", {**task, "status": task["state"]})
    return task


def _update_task(rt: Runtime, task_id: str, **fields: Any) -> dict[str, Any]:
    with locked_json(task_path(rt, task_id)) as data:
        if not data:
            raise KeyError(task_id)
        old = data.get("state")
        if "state" in fields and fields["state"] not in TASK_STATES:
            raise ValueError(f"bad task state {fields['state']!r}")
        data.update(fields)
        if fields.get("state") in ("closed", "superseded", "failed") and not data.get("closed_at"):
            data["closed_at"] = _now()
        task = dict(data)
    if task.get("state") != old:
        log_event(rt, f"task:{task['state']}", {**task, "status": task["state"]}, prev=old)
    return task


def touch_poke(runtime: Runtime | Path | str) -> None:
    rt = _rt(runtime)
    rt.run.mkdir(parents=True, exist_ok=True)
    rt.poke.touch()


# --- hold / release ---------------------------------------------------------------------

def hold_reason(runtime: Runtime | Path | str, msg: Message) -> str | None:
    """Id of the open task `msg` must wait behind, or None if it may be queued.

    Only instruct/review-request are held; from=human and --supersede bypass. msg's own task,
    and tasks whose creating message is itself still held, are not counted.
    Rule 1: the same sender→recipient pair has an open task.
    Rule 2: the sender is orchestrator and it has an open outgoing instruct (any recipient).
    """
    if msg.type not in HOLDABLE or msg.from_ == HUMAN or msg.supersedes:
        return None
    rt = _rt(runtime)
    held_ids = {m.id for m in store.held_all(rt)}
    for t in open_tasks(rt):
        if t["id"] == msg.id or t["id"] in held_ids or t["from"] != msg.from_:
            continue
        if t["to"] == msg.to:
            return t["id"]
        if msg.from_ == "orchestrator" and t["type"] == "instruct":
            return t["id"]
    return None


def release_held(runtime: Runtime | Path | str) -> list[str]:
    """Queue the oldest releasable held message, re-check the rest; repeat. Returns ids."""
    rt = _rt(runtime)
    released: list[str] = []
    with ledger_lock(rt):
        while True:
            progress = False
            for m in store.held_all(rt):
                reason = hold_reason(rt, m)
                if reason is None:
                    store.update(rt, m.id, status="queued", held_by=None)
                    released.append(m.id)
                    progress = True
                    break
                if reason != m.held_by:
                    store.update(rt, m.id, held_by=reason)
            if not progress:
                break
    if released:
        touch_poke(rt)
    return released


# --- supersede --------------------------------------------------------------------------

def _cancel_task(rt: Runtime, task: dict[str, Any], new_state: str, msg_status: str) -> bool:
    """Open task -> new_state; its undelivered message -> msg_status. False if not open."""
    if task.get("state") not in OPEN:
        return False
    _update_task(rt, task["id"], state=new_state)
    try:
        m = store.get(rt, task["id"])
    except store.MessageNotFound:
        return True
    if m.status in UNDELIVERED or (msg_status == "failed" and m.status == "delivering"):
        store.update(rt, m.id, status=msg_status, held_by=None)
    return True


def supersede_task(runtime: Runtime | Path | str, task_id: str) -> list[str]:
    """Supersede task_id and, recursively, its children (by `parent`). Returns superseded ids."""
    rt = _rt(runtime)
    done: list[str] = []
    with ledger_lock(rt):
        tasks = {t["id"]: t for t in all_tasks(rt)}
        children: dict[str, list[str]] = {}
        for t in tasks.values():
            if t.get("parent"):
                children.setdefault(t["parent"], []).append(t["id"])
        stack, seen = [task_id], set()
        while stack:
            tid = stack.pop(0)
            if tid in seen or tid not in tasks:
                continue
            seen.add(tid)
            if _cancel_task(rt, tasks[tid], "superseded", "superseded"):
                done.append(tid)
                if tid != task_id:
                    _notify_superseded(rt, tasks[tid])
            stack.extend(children.get(tid, []))
    return done


def _notify_superseded(rt: Runtime, task: dict[str, Any]) -> str | None:
    """A cascaded child whose message already reached (or is reaching) its assignee: tell the
    assignee to stop. The root task's assignee learns it from the SUPERSEDES pointer instead."""
    if task.get("to") not in AGENTS:
        return None
    try:
        m = store.get(rt, task["id"])
    except store.MessageNotFound:
        return None
    if m.status not in ("delivering", "delivered"):
        return None
    msg = _system(rt, task["to"], "superseded", f"Task {task['id']} was superseded",
                  f"Task {task['id']} was superseded; stop that work and do not report on it.\n",
                  re=task["id"])
    return msg.id


# --- system messages --------------------------------------------------------------------

def _system(rt: Runtime, to: str, result: str, subject: str, body: str,
            re: str | None = None) -> Message:
    msg = store.create(rt, from_=SYSTEM_SENDER, to=to, type="system", subject=subject, body=body,
                       re=re, result=result, status="delivered" if to == HUMAN else "queued")
    touch_poke(rt)
    return msg


# --- send -------------------------------------------------------------------------------

def _validate(rt: Runtime, *, from_: str, to: str, type: str, re: str | None,
              parent: str | None, supersede_id: str | None, result: str | None
              ) -> dict[str, Any] | None:
    """Raise LedgerError on bad input; return the replied-to task (reply types) or None."""
    parties = set(AGENTS) | {HUMAN}
    if from_ not in parties:
        raise LedgerError(f"unknown sender {from_!r}")
    if to not in parties:
        raise LedgerError(f"unknown recipient {to!r}; expected one of {', '.join([*AGENTS, HUMAN])}")
    if to == from_:
        raise LedgerError("cannot send a message to yourself")
    if type not in MSG_TYPES:
        raise LedgerError(f"unknown type {type!r}")
    if type == "system":
        raise LedgerError("system messages are generated by ads, not sent")
    try:
        validate_result(type, result)
    except ValueError as e:
        raise LedgerError(str(e)) from None
    if type in RESULT_REQUIRED and result is None:
        raise LedgerError(f"--type {type} requires --result {RESULT_HINT[type]}")
    if supersede_id and type not in TASK_TYPES:
        raise LedgerError("--supersede is only allowed with instruct, review-request or question")
    if re and type in TASK_TYPES:
        raise LedgerError(f"--re is not allowed with --type {type} (use --parent)")
    if re and type not in REPLY_TYPES:
        raise LedgerError(f"--re is only for replies (report, review, answer), not --type {type}; "
                          "mention the related task id in the subject or body instead")
    if parent:
        if type not in TASK_TYPES:
            raise LedgerError("--parent is only allowed with task-creating types")
        if get_task(rt, parent) is None:
            raise LedgerError(f"--parent {parent}: no such task")
    if supersede_id:
        t = get_task(rt, supersede_id)
        if t is None:
            raise LedgerError(f"--supersede {supersede_id}: no such task")
        if t["from"] != from_:
            raise LedgerError(f"--supersede {supersede_id}: not your task (from {t['from']})")
        if t["state"] not in OPEN:
            raise LedgerError(f"--supersede {supersede_id}: task is {t['state']}, not open")
    if type in REPLY_TYPES:
        if not re:
            raise LedgerError(f"--type {type} requires --re <task id>")
        t = get_task(rt, re)
        if t is None:
            raise LedgerError(f"--re {re}: no such task")
        if t["to"] != from_:
            raise LedgerError(f"--re {re}: that task is addressed to {t['to']}, not {from_}")
        if REPLY_TYPES[type] != t["type"]:
            raise LedgerError(f"--re {re}: a {type} answers a {REPLY_TYPES[type]}, "
                              f"but {re} is a {t['type']} (use --type {REPLY_FOR[t['type']]})")
        if to != t["from"]:
            raise LedgerError(f"--re {re}: reply must go to {t['from']}, not {to}")
        if t["state"] == "closed":
            raise LedgerError(f"--re {re}: task already closed by {t.get('reply_id')}")
        return t
    return None


def send(runtime: Runtime | Path | str, cfg: Any = None, *, from_: str, to: str, type: str,
         subject: str, body: str, re: str | None = None, parent: str | None = None,
         supersede: str | None = None, result: str | None = None,
         attachments: list[str] | None = None) -> Message:
    """The single entry point behind `ads send`. Raises LedgerError on invalid input."""
    rt = _rt(runtime)
    supersede_id = supersede
    with ledger_lock(rt):
        replied = _validate(rt, from_=from_, to=to, type=type, re=re, parent=parent,
                            supersede_id=supersede_id, result=result)
        late = replied is not None and replied["state"] in ("superseded", "failed")
        held_by = None
        if late:
            status = "ignored"
        elif to == HUMAN:
            status = "delivered"
        else:
            probe = Message(id="m-00000000-000000", seq=0, from_=from_, to=to, type=type,
                            supersedes=supersede_id)
            held_by = hold_reason(rt, probe)
            status = "held" if held_by else "queued"
        msg = store.create(rt, from_=from_, to=to, type=type, body=body, subject=subject, re=re,
                           parent=parent, supersedes=supersede_id, status=status,
                           held_by=held_by, result=result, attachments=attachments,
                           **({"late_reply_to": replied["state"]} if late else {}))
        if type in TASK_TYPES:
            _create_task(rt, msg)
        changed = False
        if supersede_id:
            changed |= bool(supersede_task(rt, supersede_id))
        if replied is not None and not late:
            _update_task(rt, replied["id"], state="closed", reply_id=msg.id)
            changed = True
        if from_ in AGENTS and (type in REPLY_TYPES or type in TASK_TYPES):
            agent_state.transition(rt, from_, "progress")
        if changed:
            release_held(rt)
    touch_poke(rt)
    return msg


# --- delivery ---------------------------------------------------------------------------

def mark_delivered(runtime: Runtime | Path | str, msg_id: str) -> Message | None:
    """Message -> delivered (from delivering, or queued via delivering) and its task
    queued -> delivered. Idempotent. Returns the message (None if unknown)."""
    rt = _rt(runtime)
    with ledger_lock(rt):
        try:
            m = store.get(rt, msg_id)
        except store.MessageNotFound:
            return None
        if m.status == "queued":
            m = store.update(rt, msg_id, status="delivering")
        if m.status == "delivering":
            m = store.update(rt, msg_id, status="delivered")
        t = get_task(rt, msg_id)
        if t is not None and t["state"] == "queued" and m.status == "delivered":
            _update_task(rt, msg_id, state="delivered")
    return m


# --- cascades ---------------------------------------------------------------------------

def agent_down_cascade(runtime: Runtime | Path | str, agent: str) -> list[str]:
    """On down(pane_dead): fail the agent's open incoming tasks, notify each sender with
    system(agent-down), then release held messages. Returns the system message ids."""
    rt = _rt(runtime)
    out: list[str] = []
    with ledger_lock(rt):
        for t in open_incoming(rt, agent):
            _cancel_task(rt, t, "failed", "failed")
            m = _system(rt, t["from"], "agent-down", f"{agent} is down; task {t['id']} failed",
                        f"Agent {agent} went down (pane dead). Your {t['type']} {t['id']} "
                        f"(\"{t.get('subject', '')}\") was marked failed. Resend it after "
                        f"`ads restart {agent}` if still needed.\n", re=t["id"])
            out.append(m.id)
        release_held(rt)
    return out


def _reply_command(rt: Runtime, agent: str, t: dict[str, Any]) -> str:
    rtype = REPLY_FOR[t["type"]]
    from ads.launcher import ads_bin  # lazy: only needed when a Stop is blocked
    parts = [f"{ads_bin(rt)} send --to {t['from']} --type {rtype} --re {t['id']}"]
    if rtype in RESULT_HINT:
        parts.append(f"--result {RESULT_HINT[rtype]}")
    subject = (t.get("subject") or "").replace('"', "'")
    parts.append(f'--subject "Re: {subject}"' if subject else '--subject "…"')
    parts.append(f"--body-file {rt.agent_dir(agent) / ('reply-' + t['id'] + '.md')}")
    return " ".join(parts)


def stop_decision(runtime: Runtime | Path | str, cfg: Any, agent: str) -> str | None:
    """Block reason for the Stop hook, or None to allow the stop (plan §4.6)."""
    rt = _rt(runtime)
    max_nudges = cfg.protocol.max_report_nudges
    with ledger_lock(rt):
        tasks = [t for t in open_incoming(rt, agent) if t["state"] == "delivered"]
        if not tasks:
            return None
        if agent_state.read_state(rt, agent).get("progress_this_turn", 0):
            return None
        if open_outgoing(rt, agent):
            return None
        if min(t.get("nudges", 0) for t in tasks) >= max_nudges:
            return None
        for t in tasks:
            _update_task(rt, t["id"], nudges=t.get("nudges", 0) + 1)
    lines = [f"ADS: you have {len(tasks)} open task(s) without a reply. Finish the work, "
             "write the reply body to the file, then run the command (one per task):"]
    for t in tasks:
        lines.append(f"- {t['id']} ({t['type']} from {t['from']}): {_reply_command(rt, agent, t)}")
    lines.append("If you delegated or asked a question, that counts; use --result failure "
                 "if you cannot complete a task.")
    return "\n".join(lines)


def exhausted_tasks(runtime: Runtime | Path | str, cfg: Any) -> list[dict[str, Any]]:
    """Open delivered tasks whose nudges reached max_report_nudges."""
    max_nudges = cfg.protocol.max_report_nudges
    return [t for t in open_tasks(runtime)
            if t["state"] == "delivered" and t.get("nudges", 0) >= max_nudges]


def fail_missing_report(runtime: Runtime | Path | str, task: dict[str, Any]) -> str | None:
    """Mark task failed and send system(missing-report) to its sender. Returns the msg id."""
    rt = _rt(runtime)
    with ledger_lock(rt):
        current = get_task(rt, task["id"])
        if current is None or not _cancel_task(rt, current, "failed", "failed"):
            return None
        m = _system(rt, current["from"], "missing-report",
                    f"no reply from {current['to']} for {current['id']}",
                    f"{current['to']} ended its turn without replying to your {current['type']} "
                    f"{current['id']} (\"{current.get('subject', '')}\") after the nudge limit. "
                    "The task is marked failed.\n", re=current["id"])
        release_held(rt)
    return m.id


def api_error_notify(runtime: Runtime | Path | str, agent: str, error: Any) -> list[str]:
    """system(api-error) to the sender of each open incoming task of `agent`."""
    rt = _rt(runtime)
    out: list[str] = []
    text = error if isinstance(error, str) else json.dumps(error, default=str)
    with ledger_lock(rt):
        for t in open_incoming(rt, agent):
            m = _system(rt, t["from"], "api-error", f"{agent} hit an API error on {t['id']}",
                        f"Agent {agent} stopped with an API error while handling your "
                        f"{t['type']} {t['id']}: {text}\n", re=t["id"])
            out.append(m.id)
    return out


def current_phase(runtime: Runtime | Path | str) -> str | None:
    """plan|dev|test from the orchestrator's oldest open, non-held outgoing instruct."""
    rt = _rt(runtime)
    held_ids = {m.id for m in store.held_all(rt)}
    for t in open_outgoing(rt, "orchestrator"):
        if t["type"] == "instruct" and t["id"] not in held_ids:
            return PHASES.get(t["to"])
    return None
