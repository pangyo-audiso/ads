"""Message store: `work/msgs/<id>.json` envelopes + `<id>.md` bodies, global seq (plan §4.4).

Task/ledger logic lives in `bus/ledger.py` (M1c), not here.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from ads.bus import envelope as env
from ads.bus.envelope import Message
from ads.bus.log import log_event
from ads.paths import Runtime, locked_json

# Legal status transitions. Terminal statuses map to an empty set.
# `failed -> queued` is the `ads send --requeue <id>` path; `delivering -> queued` is
# the supervisor-restart recovery of a stale in-flight paste.
TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"held", "delivering", "superseded", "failed", "ignored"}),
    "held": frozenset({"queued", "superseded", "failed", "ignored"}),
    "delivering": frozenset({"delivered", "queued", "failed"}),
    "delivered": frozenset(),
    "failed": frozenset({"queued"}),
    "superseded": frozenset(),
    "ignored": frozenset(),
}
TERMINAL: frozenset[str] = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)

# Statuses a message may be created with (to=human → delivered; late reply → ignored).
INITIAL_STATUSES: frozenset[str] = frozenset({"queued", "held", "delivered", "ignored"})

_IMMUTABLE = frozenset({"id", "seq", "from_", "from", "created"})
_FIELDS = frozenset(Message.__dataclass_fields__)


class TransitionError(ValueError):
    """Illegal message status transition."""


class MessageNotFound(KeyError):
    """No envelope for that id."""


def _rt(runtime: Runtime | Path | str) -> Runtime:
    return runtime if isinstance(runtime, Runtime) else Runtime(Path(runtime))


def can_transition(old: str, new: str) -> bool:
    return new in TRANSITIONS.get(old, frozenset())


def envelope_path(runtime: Runtime | Path | str, msg_id: str) -> Path:
    return _rt(runtime).msgs / f"{msg_id}.json"


def body_path(runtime: Runtime | Path | str, msg_id: str) -> Path:
    return env.body_path(_rt(runtime), msg_id)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- seq ----------------------------------------------------------------------------

def _max_stored_seq(rt: Runtime) -> int:
    best = 0
    for m in all_messages(rt):
        best = max(best, m.seq)
    return best


def next_seq(runtime: Runtime | Path | str) -> int:
    """Next value of the global monotonic counter `work/run/seq` (flock; never resets).

    If the counter file is missing, it is seeded from the highest stored message seq.
    """
    rt = _rt(runtime)
    rt.run.mkdir(parents=True, exist_ok=True)
    lock_path = rt.seq.with_name(rt.seq.name + ".lock")
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                current = int(rt.seq.read_text().strip() or 0)
            except FileNotFoundError:
                current = _max_stored_seq(rt)
            value = current + 1
            _atomic_write(rt.seq, f"{value}\n")
            return value
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


# --- create / get / update ------------------------------------------------------------

def create(
    runtime: Runtime | Path | str,
    *,
    from_: str,
    to: str,
    type: str,
    body: str,
    subject: str = "",
    re: str | None = None,
    parent: str | None = None,
    supersedes: str | None = None,
    expects_reply: bool | None = None,
    status: str = "queued",
    held_by: str | None = None,
    result: str | None = None,
    attachments: list[str] | None = None,
    now: datetime | None = None,
    **log_extra: Any,
) -> Message:
    """Allocate a seq, write `<id>.md` then `<id>.json` (atomic), log `created`."""
    rt = _rt(runtime)
    if status not in INITIAL_STATUSES:
        raise TransitionError(f"cannot create a message with status {status!r}")
    now = now or datetime.now().astimezone()
    seq = next_seq(rt)
    msg = Message(
        id=env.make_id(seq, now),
        seq=seq,
        from_=from_,
        to=to,
        type=type,
        re=re,
        parent=parent,
        supersedes=supersedes,
        subject=env.sanitize_subject(subject),
        created=now.isoformat(timespec="seconds"),
        expects_reply=(type in env.TASK_TYPES) if expects_reply is None else bool(expects_reply),
        status=status,
        held_by=held_by,
        result=result,
        attachments=[str(a) for a in (attachments or [])],
        delivered_at=now.isoformat(timespec="seconds") if status == "delivered" else None,
    )
    msg.validate()
    _atomic_write(body_path(rt, msg.id), body if body.endswith("\n") or not body else body + "\n")
    _atomic_write(envelope_path(rt, msg.id),
                  json.dumps(msg.to_dict(), ensure_ascii=False, indent=1) + "\n")
    log_event(rt, "created", msg, **log_extra)
    return msg


def get(runtime: Runtime | Path | str, msg_id: str) -> Message:
    try:
        data = json.loads(envelope_path(runtime, msg_id).read_text())
    except FileNotFoundError:
        raise MessageNotFound(msg_id) from None
    return Message.from_dict(data)


def read_body(runtime: Runtime | Path | str, msg_id: str) -> str:
    return body_path(runtime, msg_id).read_bytes().decode("utf-8")


def update(runtime: Runtime | Path | str, msg_id: str, *, log_extra: dict[str, Any] | None = None,
           _bump: dict[str, int] | None = None, **fields: Any) -> Message:
    """Atomically update envelope fields under `locked_json`.

    A `status` change must be legal per TRANSITIONS (same status = no transition) and is
    logged to bus.jsonl as event `status:<new>`. Entering `delivered` stamps `delivered_at`.
    """
    rt = _rt(runtime)
    if "from" in fields:
        fields["from_"] = fields.pop("from")
    bad = set(fields) - _FIELDS
    if bad:
        raise ValueError(f"unknown message fields: {sorted(bad)}")
    frozen = set(fields) & _IMMUTABLE
    if frozen:
        raise ValueError(f"immutable message fields: {sorted(frozen)}")
    if _bump and not set(_bump) <= {"enters", "pastes"}:
        raise ValueError(f"only enters/pastes can be bumped: {sorted(_bump)}")
    if "subject" in fields:
        fields["subject"] = env.sanitize_subject(fields["subject"])
    path = envelope_path(rt, msg_id)
    if not path.exists():
        raise MessageNotFound(msg_id)
    with locked_json(path) as data:
        if not data:
            raise MessageNotFound(msg_id)
        msg = Message.from_dict(data)
        old = msg.status
        new = fields.get("status", old)
        if new != old and not can_transition(old, new):
            raise TransitionError(f"{msg_id}: illegal transition {old} -> {new}")
        for k, v in fields.items():
            setattr(msg, k, v)
        for k, by in (_bump or {}).items():
            setattr(msg, k, getattr(msg, k) + by)
        if new == "delivered" and old != "delivered" and not msg.delivered_at:
            msg.delivered_at = env.now_iso()
        msg.validate()
        data.clear()
        data.update(msg.to_dict())
    if new != old:
        log_event(rt, f"status:{new}", msg, prev=old, **(log_extra or {}))
    return msg


def bump(runtime: Runtime | Path | str, msg_id: str, field: str, by: int = 1,
         **fields: Any) -> Message:
    """Atomically increment `enters` or `pastes` (plus optional other field updates)."""
    return update(runtime, msg_id, _bump={field: by}, **fields)


# --- queries --------------------------------------------------------------------------

def all_messages(runtime: Runtime | Path | str) -> list[Message]:
    """Every stored message, sorted by seq."""
    rt = _rt(runtime)
    out = []
    for p in rt.msgs.glob("m-*.json"):
        try:
            out.append(Message.from_dict(json.loads(p.read_text())))
        except (OSError, ValueError, TypeError):
            continue
    out.sort(key=lambda m: m.seq)
    return out


def queued_for(runtime: Runtime | Path | str, agent: str) -> list[Message]:
    return [m for m in all_messages(runtime) if m.to == agent and m.status == "queued"]


def held_all(runtime: Runtime | Path | str) -> list[Message]:
    return [m for m in all_messages(runtime) if m.status == "held"]


def inflight_for(runtime: Runtime | Path | str, agent: str) -> list[Message]:
    return [m for m in all_messages(runtime) if m.to == agent and m.status == "delivering"]
