"""Append-only bus event log `work/logs/bus.jsonl` (one JSON line per event)."""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ads.paths import ProjectState, StateLike, as_state


def _rt(state: StateLike) -> ProjectState:
    return as_state(state)


def log_path(state: StateLike) -> Path:
    return _rt(state).logs / "bus.jsonl"


def log_event(state: StateLike, event: str, msg: Any = None, **extra: Any) -> None:
    """Append {ts, event, id, from, to, type, status, extra} for `msg` (Message, dict or None)."""
    if msg is not None and not isinstance(msg, dict):
        msg = msg.to_dict()
    msg = msg or {}
    rec = {
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "event": event,
        "id": msg.get("id"),
        "from": msg.get("from", msg.get("from_")),
        "to": msg.get("to"),
        "type": msg.get("type"),
        "status": msg.get("status"),
        "extra": extra,
    }
    line = (json.dumps(rec, ensure_ascii=False, default=str) + "\n").encode()
    path = log_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            view = memoryview(line)
            while view:
                n = os.write(fd, view)
                view = view[n:]
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def read_events(state: StateLike) -> list[dict[str, Any]]:
    """All parseable events in order (malformed lines are skipped)."""
    try:
        text = log_path(state).read_text()
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
