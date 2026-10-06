"""Startup-dialog watchdog table and screen predicates (plan §4.3, corrected by the M0.5 spike).

Ground truth: `tests/fixtures/dialogs/*.txt` captured from Claude Code 2.1.289
(`docs/spike-claude.md`). Key facts:

- Dialog options are NOT numbered; the default (`❯`) is "No, exit". To accept: Down,
  re-capture until `❯` is on the accept line, then Enter (≤ 3 tries).
- A dialog screen ends with the footer `Enter to confirm · Esc to cancel` and has no input box.
- The idle/busy input box line is `❯` + U+00A0 (NO-BREAK SPACE) right below a `─` rule.
  Dialog option markers and transcript echoes use `❯` + ASCII space. Python's `\\s` matches
  U+00A0, so the input-box test runs on raw lines and never whitespace-normalizes first.
- Busy: `esc to interrupt` in the status bar (the input box stays visible while busy), or a
  spinner line (`* · ✢ ✶ ✽` + verb + `…`) just above the input box.
- A resumed session re-renders old transcript, which may contain dialog text. Matching is
  therefore anchored on the last TAIL_LINES non-blank-trailing lines, requires the footer to be
  the last non-blank line, and requires the absence of the input box; the supervisor
  additionally gates the watchdog by agent state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TAIL_LINES = 15
FOOTER = "Enter to confirm · Esc to cancel"
INPUT_BOX_RE = re.compile("^❯ ")          # raw line, no normalization
RULE_RE = re.compile("^─{20,}")
BUSY_MARKER = "esc to interrupt"
SPINNER_GLYPHS = "*·✢✶✽"
SPINNER_RE = re.compile(f"^[{re.escape(SPINNER_GLYPHS)}] \\S.*…")


@dataclass(frozen=True)
class Dialog:
    """One watchdog entry.

    detect   all of these must match (re.M) the tail text
    accept   regex for "❯ is on the accept option"; when set, answer = Down until it
             matches (≤ 3 tries) then Enter. When None, answer = `keys`.
    """

    name: str
    detect: tuple[re.Pattern[str], ...]
    accept: re.Pattern[str] | None = None
    keys: tuple[str, ...] = ("Enter",)
    needs_footer: bool = True
    max_hits_per_min: int = 3
    fixtures: tuple[str, ...] = field(default=())


def _opt(text: str) -> str:
    """An option line, selected or not: leading spaces, optional `❯ `, the text."""
    return f"^ *(?:❯ +)?{re.escape(text)} *$"


def _selected(text: str) -> re.Pattern[str]:
    return re.compile(f"^ *❯ +{re.escape(text)} *$", re.M)


_MARKED_EXIT_OR = "^ *❯ +(?:No, exit|{}) *$"

DIALOGS: tuple[Dialog, ...] = (
    Dialog(
        name="trust",
        detect=(re.compile(_opt("Yes, I trust this folder"), re.M),
                re.compile(_opt("No, exit"), re.M),
                re.compile(_MARKED_EXIT_OR.format(re.escape("Yes, I trust this folder")), re.M)),
        accept=_selected("Yes, I trust this folder"),
        fixtures=("trust.txt", "trust_selected.txt"),
    ),
    Dialog(
        name="bypass",
        detect=(re.compile(_opt("Yes, I accept"), re.M),
                re.compile(_opt("No, exit"), re.M),
                re.compile(_MARKED_EXIT_OR.format(re.escape("Yes, I accept")), re.M)),
        accept=_selected("Yes, I accept"),
        fixtures=("bypass.txt", "bypass_selected.txt"),
    ),
    # Not observed in the spike (account already onboarded): tolerant guess at the first-run
    # theme picker. Enter keeps the pre-selected theme. No fixture.
    Dialog(
        name="theme",
        detect=(re.compile(r"^ *❯ +(?:\d+\. +)?Dark mode", re.M),
                re.compile(r"(?:text style|theme)", re.M | re.I)),
        keys=("Enter",),
        needs_footer=False,
    ),
)

BY_NAME: dict[str, Dialog] = {d.name: d for d in DIALOGS}


# --- screen helpers ---------------------------------------------------------------------

def _lines(raw: str) -> list[str]:
    """Raw lines with ASCII-space/CR right-trimmed (U+00A0 kept), trailing blank lines dropped."""
    lines = [ln.rstrip(" \r\t") for ln in raw.split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def tail(raw: str, n: int = TAIL_LINES) -> list[str]:
    return _lines(raw)[-n:]


def has_input_box(raw: str, n: int = TAIL_LINES) -> bool:
    """True if Claude's input box (`❯\\xa0` line right below a `─` rule) is in the tail."""
    lines = tail(raw, n)
    for i, ln in enumerate(lines):
        if INPUT_BOX_RE.match(ln) and i > 0 and RULE_RE.match(lines[i - 1]):
            return True
    return False


def _input_box_index(lines: list[str]) -> int | None:
    for i in range(len(lines) - 1, 0, -1):
        if INPUT_BOX_RE.match(lines[i]) and RULE_RE.match(lines[i - 1]):
            return i
    return None


def input_box_text(raw: str, n: int = TAIL_LINES) -> str | None:
    """Text inside the input box (all lines up to the closing rule), or None if no box."""
    lines = tail(raw, n)
    i = _input_box_index(lines)
    if i is None:
        return None
    out = [lines[i][2:]]
    for ln in lines[i + 1:]:
        if RULE_RE.match(ln):
            break
        out.append(ln)
    return "\n".join(out)


def is_busy(raw: str, n: int = TAIL_LINES) -> bool:
    """Busy marker in the status area below the input box, or a spinner line above it."""
    lines = tail(raw, n)
    i = _input_box_index(lines)
    if i is None:
        status = lines[-3:]
        return any(BUSY_MARKER in ln for ln in status)
    below = lines[i + 1:]
    if any(BUSY_MARKER in ln for ln in below):
        return True
    above = [ln for ln in lines[max(0, i - 4):i - 1] if ln.strip()]
    return bool(above) and bool(SPINNER_RE.match(above[-1]))


def footer_last(raw: str) -> bool:
    lines = _lines(raw)
    return bool(lines) and lines[-1].strip() == FOOTER


def match_dialog(raw: str) -> Dialog | None:
    """The known dialog on screen, or None. Never matches when the input box is visible."""
    if has_input_box(raw):
        return None
    text = "\n".join(tail(raw))
    is_footer = footer_last(raw)
    for d in DIALOGS:
        if d.needs_footer and not is_footer:
            continue
        if all(p.search(text) for p in d.detect):
            return d
    return None


def is_unknown_dialog(raw: str) -> bool:
    """A dialog-shaped screen (footer last, no input box) that no table entry knows."""
    return footer_last(raw) and not has_input_box(raw) and match_dialog(raw) is None


def accept_selected(dialog: Dialog, raw: str) -> bool:
    """True when `❯` is on the dialog's accept option (always True for key-only dialogs)."""
    if dialog.accept is None:
        return True
    return bool(dialog.accept.search("\n".join(tail(raw))))
