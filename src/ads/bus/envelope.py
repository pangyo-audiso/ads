"""Message envelope, protocol constants and the one-line pane pointer (plan §4.4, §5)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ads.paths import Runtime

# --- protocol constants ---------------------------------------------------------

MSG_TYPES: frozenset[str] = frozenset({
    "instruct", "report", "review-request", "review", "question", "answer", "info", "system",
})

# Types that create a ledger task.
TASK_TYPES: frozenset[str] = frozenset({"instruct", "review-request", "question"})

# reply type -> the task type it closes.
REPLY_TYPES: dict[str, str] = {
    "report": "instruct",
    "review": "review-request",
    "answer": "question",
}

# Allowed `result` values per type (types absent here take no result).
RESULTS: dict[str, frozenset[str]] = {
    "report": frozenset({"success", "partial", "failure"}),
    "review": frozenset({"pass", "revise"}),
    "system": frozenset({"agent-down", "missing-report", "api-error", "superseded"}),
}

STATUSES: frozenset[str] = frozenset({
    "queued", "held", "delivering", "delivered", "failed", "superseded", "ignored",
})

NAME_RE = re.compile(r"^[\w-]+$")
ID_RE = re.compile(r"^m-\d{8}-\d{6,}$")

POINTER_RE = re.compile(
    r'^\[ADS-MSG id=(?P<id>m-\d{8}-\d{6,}) from=(?P<from>[\w-]+) type=(?P<type>[\w-]+)\]'
)
SUPERSEDES_RE = re.compile(r" SUPERSEDES (?P<supersedes>m-\d{8}-\d{6,}): abort that task first\.")

POINTER_MAX = 400
SUBJECT_MAX = 200


# --- helpers ----------------------------------------------------------------------

def make_id(seq: int, day: date | datetime | None = None) -> str:
    """`m-YYYYMMDD-<seq:06d>` (seq may exceed 6 digits)."""
    if seq < 0:
        raise ValueError(f"negative seq: {seq}")
    day = day or datetime.now().astimezone()
    return f"m-{day.strftime('%Y%m%d')}-{seq:06d}"


def sanitize_subject(subject: str | None) -> str:
    """Single line, no control/format/separator characters, collapsed whitespace, ≤ 200 chars."""
    if not subject:
        return ""
    out = []
    for ch in str(subject):
        cat = unicodedata.category(ch)
        if ch.isspace() or cat in ("Zl", "Zp"):
            out.append(" ")
        elif cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        else:
            out.append(ch)
    text = re.sub(r" +", " ", "".join(out)).strip()
    if len(text) > SUBJECT_MAX:
        text = text[: SUBJECT_MAX - 1].rstrip() + "…"
    return text


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --- envelope ---------------------------------------------------------------------

@dataclass
class Message:
    id: str
    seq: int
    from_: str
    to: str
    type: str
    re: str | None = None
    parent: str | None = None
    supersedes: str | None = None
    subject: str = ""
    created: str = ""
    expects_reply: bool = False
    status: str = "queued"
    held_by: str | None = None
    result: str | None = None
    attachments: list[str] = field(default_factory=list)
    delivered_at: str | None = None
    enters: int = 0
    pastes: int = 0
    unconfirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for f in fields(self):
            key = "from" if f.name == "from_" else f.name
            val = getattr(self, f.name)
            d[key] = list(val) if isinstance(val, list) else val
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        known = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, val in data.items():
            name = "from_" if key in ("from", "from_") else key
            if name in known:
                kwargs[name] = val
        if kwargs.get("attachments") is None:
            kwargs["attachments"] = []
        return cls(**kwargs)

    def validate(self) -> None:
        """Raise ValueError if the envelope is malformed."""
        if not ID_RE.match(self.id):
            raise ValueError(f"bad message id: {self.id!r}")
        for label, name in (("from", self.from_), ("to", self.to)):
            if not isinstance(name, str) or not NAME_RE.match(name):
                raise ValueError(f"bad {label} name: {name!r}")
        if self.type not in MSG_TYPES:
            raise ValueError(f"unknown message type: {self.type!r}")
        if self.status not in STATUSES:
            raise ValueError(f"unknown status: {self.status!r}")
        validate_result(self.type, self.result)
        for label, ref in (("re", self.re), ("parent", self.parent),
                           ("supersedes", self.supersedes)):
            if ref is not None and not ID_RE.match(ref):
                raise ValueError(f"bad {label} id: {ref!r}")


def validate_result(msg_type: str, result: str | None) -> None:
    if result is None:
        return
    allowed = RESULTS.get(msg_type)
    if not allowed:
        raise ValueError(f"type {msg_type!r} takes no result (got {result!r})")
    if result not in allowed:
        raise ValueError(f"bad result {result!r} for {msg_type}; expected one of {sorted(allowed)}")


# --- pointer ----------------------------------------------------------------------

def body_path(runtime: Runtime | Path | str, msg_id: str) -> Path:
    rt = runtime if isinstance(runtime, Runtime) else Runtime(Path(runtime))
    return rt.msgs / f"{msg_id}.md"


def pointer_line(msg: Message, runtime: Runtime | Path | str) -> str:
    """The single line pasted into the recipient's pane (no newline, ≤ 400 chars)."""
    line = (f"[ADS-MSG id={msg.id} from={msg.from_} type={msg.type}] "
            f"Read {body_path(runtime, msg.id)} and follow the ADS protocol.")
    if msg.supersedes:
        line += f" SUPERSEDES {msg.supersedes}: abort that task first."
    if "\n" in line or "\r" in line:
        raise ValueError("pointer would contain a newline (runtime path?)")
    if len(line) > POINTER_MAX:
        raise ValueError(f"pointer is {len(line)} chars (> {POINTER_MAX}); runtime path too long")
    if not POINTER_RE.match(line):
        raise ValueError(f"pointer does not match POINTER_RE: {line!r}")
    return line


def parse_pointer(text: str | None) -> dict[str, str | None] | None:
    """Parse a pointer at the start of `text` (leading whitespace ignored).

    Returns {"id", "from", "type", "supersedes"} or None.
    """
    if not text:
        return None
    first = text.lstrip().split("\n", 1)[0]
    m = POINTER_RE.match(first)
    if not m:
        return None
    sup = SUPERSEDES_RE.search(first, m.end())
    return {"id": m["id"], "from": m["from"], "type": m["type"],
            "supersedes": sup["supersedes"] if sup else None}
