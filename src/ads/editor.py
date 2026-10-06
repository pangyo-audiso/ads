"""Pane-3 human editor (plan §9): a prompt_toolkit prompt that talks to the orchestrator.

Run as `ads input` in tmux window 0 pane 3 (`main(runtime, cfg)`).

Keys
----
- **Enter** submits (insert *and* vi navigation mode; not while C-r / `/` searching, where
  Enter accepts the search). Blank input is ignored.
  * If the orchestrator has an open `question` to the human, the text is sent as
    `answer --re <qid>` to the orchestrator (toolbar shows "answering Q m-…").
  * Otherwise it is sent as `instruct --to orchestrator` (subject = first line, ≤ 80 chars).
  * A leading `!` (or `/ads instruct <text>`) forces an instruct even while a question is open.
- **Newline**: C-j and Alt+Enter (`Esc Enter`) always insert a newline. Because Alt+Enter
  is the byte pair ESC CR, pressing Esc and then Enter within ESC_TIMEOUT_S (0.3 s) also
  reads as Alt+Enter; after a short pause Enter submits from vi navigation mode.
  **Shift+Enter** reaches us as C-j: the ads tmux config binds
  `S-Enter if-shell -F '#{==:#{@ads_role},human}' 'send-keys C-j' 'send-keys S-Enter'`
  (with `extended-keys on`), so nothing extra is needed here. `ads doctor --key-probe`
  (→ `key_probe()`) or `/ads keys` shows what the terminal actually sends.
- **History**: FileHistory at `cfg.editor.history_file` (relative to the runtime).
  Up/Down (and vi `k`/`j` in navigation mode) move inside a multi-line buffer and walk
  history at its first/last line (prompt_toolkit auto_up/auto_down); with history search
  enabled, Up only recalls entries starting with the text before the cursor.
- **Search**: C-r reverse incremental search (works in vi insert mode too), vi `/ ? n N`.
- **C-c** clears the buffer; C-c twice within 1 s exits (return 0). C-d is a no-op
  (so the pane is not lost by accident).

Local commands (whole input starting with `/ads`): status, inbox, keys, restart
<agent>|supervisor [--resume], instruct <text>, help.

New messages to the human (reports, questions, system notices) are printed above the prompt.
Logs go to `work/logs/editor.log`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import select
import sys
import termios
import time
import tty
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ads.bus import ledger, store
from ads.bus import state as agent_state
from ads.bus.envelope import Message
from ads.paths import AGENTS, HUMAN, Runtime

log = logging.getLogger("ads.editor")

ORCH = "orchestrator"
SUBJECT_LEN = 80
EXIT_WINDOW_S = 1.0
TICK_S = 1.0
ESC_TIMEOUT_S = 0.3
EXCERPT_LINES = 20
EXCERPT_CHARS = 1500

AGENT_ABBR = {
    "orchestrator": "orch", "planner": "plan", "tester": "test", "evaluator": "eval",
    "developer": "dev", "coder-1": "c1", "coder-2": "c2",
}
STATE_ABBR = {"starting": "start", "continuing": "cont", "restarting": "rst", "dialog": "dlg"}

HELP = """\
ads editor — Enter: send to orchestrator (answers its open question if any)
  C-j / Alt+Enter / Shift+Enter : newline        C-r : reverse history search
  Up/Down (vi k/j)              : history        C-c : clear, twice within 1 s: exit
  !<text>                       : force an instruct while a question is open
/ads status                     : agent states, queues, tasks, phase
/ads inbox                      : messages to you (open questions marked)
/ads keys                       : show raw bytes of the keys you press (5 s)
/ads restart <agent>|supervisor [--resume] : ask the supervisor to restart
/ads instruct <text>            : send an instruct even while a question is open
/ads help                       : this help"""


def _rt(runtime: Runtime | Path | str) -> Runtime:
    return runtime if isinstance(runtime, Runtime) else Runtime(Path(runtime))


def setup_logging(runtime: Runtime | Path | str) -> None:
    rt = _rt(runtime)
    path = rt.logs / "editor.log"
    for h in log.handlers:
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == path:
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    h = logging.FileHandler(path)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


# --- queries --------------------------------------------------------------------------

def open_question(runtime: Runtime | Path | str) -> dict[str, Any] | None:
    """Oldest open question task from the orchestrator to the human, or None."""
    for t in ledger.open_tasks(runtime):
        if t.get("type") == "question" and t.get("from") == ORCH and t.get("to") == HUMAN:
            return t
    return None


def human_messages(runtime: Runtime | Path | str) -> list[Message]:
    """Messages addressed to the human, by seq (oldest first)."""
    return [m for m in store.all_messages(runtime) if m.to == HUMAN]


def supervisor_alive(runtime: Runtime | Path | str) -> bool:
    try:
        m = re.search(r"\d+", _rt(runtime).supervisor_pid.read_text())
        if not m:
            return False
        os.kill(int(m.group()), 0)
        return True
    except (OSError, ValueError):
        return False


def _pending_supersede(msgs: list[Message]) -> list[Message]:
    return [m for m in msgs if m.supersedes and m.status in ("queued", "held", "delivering")]


def _trunc(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def subject_of(text: str) -> str:
    """First non-blank line, ≤ SUBJECT_LEN chars."""
    for line in text.splitlines():
        if line.strip():
            return _trunc(line.strip(), SUBJECT_LEN)
    return ""


# --- toolbar --------------------------------------------------------------------------

def toolbar_lines(runtime: Runtime | Path | str) -> list[str]:
    """Two compact status lines for the bottom toolbar."""
    rt = _rt(runtime)
    states = agent_state.all_states(rt)
    parts = []
    for a in AGENTS:
        st = states.get(a, {}).get("state") or "?"
        parts.append(f"{AGENT_ABBR.get(a, a)}:{STATE_ABBR.get(st, st)}")
    msgs = store.all_messages(rt)
    queued = sum(1 for m in msgs if m.status == "queued")
    held = sum(1 for m in msgs if m.status == "held")
    line1 = " ".join(parts) + f" | phase:{ledger.current_phase(rt) or '-'} | q:{queued} h:{held}"
    if _pending_supersede(msgs):
        line1 += " | supersede pending"
    extras = []
    q = open_question(rt)
    if q:
        extras.append(f"answering Q {q['id']}")
    to_human = [m for m in msgs if m.to == HUMAN]
    if to_human:
        m = to_human[-1]
        kind = m.type + (f"/{m.result}" if m.result else "")
        extras.append(f"last: {kind} from {m.from_}: {_trunc(m.subject, 50)}")
    return [line1, " | ".join(extras)]


# --- status / inbox renderers ---------------------------------------------------------

def render_status(runtime: Runtime | Path | str, cfg: Any = None) -> str:
    rt = _rt(runtime)
    states = agent_state.all_states(rt)
    out = ["agents:"]
    for a in AGENTS:
        st = states.get(a, {})
        model = ""
        if cfg is not None and a in getattr(cfg, "agents", {}):
            model = cfg.agents[a].model
        reason = st.get("reason") or ""
        infl = st.get("inflight_msg") or ""
        out.append(f"  {a:<13}{st.get('state', '?'):<11}{reason:<16}{model:<20}{infl}".rstrip())
    msgs = store.all_messages(rt)
    pending = [m for m in msgs if m.status in ("queued", "held", "delivering")]
    out.append(f"queue: {len(pending)} pending")
    for m in pending:
        held = f" held_by={m.held_by}" if m.held_by else ""
        out.append(f"  {m.id} {m.status:<10} {m.from_}->{m.to} {m.type}{held}  "
                   f"{_trunc(m.subject, 50)}")
    tasks = ledger.open_tasks(rt)
    out.append(f"open tasks: {len(tasks)}")
    for t in tasks:
        nudges = f" nudges={t.get('nudges')}" if t.get("nudges") else ""
        out.append(f"  {t['id']} {t['state']:<9} {t['from']}->{t['to']} {t['type']}{nudges}  "
                   f"{_trunc(t.get('subject') or '', 50)}")
    for m in _pending_supersede(msgs):
        out.append(f"supersede pending: {m.id} supersedes {m.supersedes}")
    out.append(f"phase: {ledger.current_phase(rt) or '-'}")
    out.append(f"supervisor: {'alive' if supervisor_alive(rt) else 'NOT running'}")
    alerts = list(rt.alerts.glob("*")) if rt.alerts.is_dir() else []
    if alerts:
        out.append(f"alerts: {len(alerts)} unprocessed")
    return "\n".join(out)


def render_inbox(runtime: Runtime | Path | str, limit: int = 20) -> str:
    rt = _rt(runtime)
    msgs = human_messages(rt)
    if not msgs:
        return "inbox: no messages to human yet"
    open_q = {t["id"] for t in ledger.open_tasks(rt)
              if t.get("type") == "question" and t.get("to") == HUMAN}
    out = [f"inbox: {len(msgs)} message(s){' (last %d)' % limit if len(msgs) > limit else ''}"]
    for m in msgs[-limit:]:
        kind = m.type + (f"/{m.result}" if m.result else "")
        mark = " [OPEN QUESTION]" if m.id in open_q else ""
        re_ = f" re={m.re}" if m.re else ""
        out.append(f"  {m.id} {m.created[11:19] if m.created else ''} {kind:<16} "
                   f"from {m.from_}{re_}{mark}: {_trunc(m.subject, 60)}")
    return "\n".join(out)


def render_incoming(runtime: Runtime | Path | str, m: Message) -> str:
    """Block printed above the prompt when a new message to the human arrives."""
    rt = _rt(runtime)
    kind = m.type + (f"/{m.result}" if m.result else "")
    head = f"── {m.id} {kind} from {m.from_}" + (f" re={m.re}" if m.re else "") + " ──"
    try:
        body = store.read_body(rt, m.id).rstrip()
    except OSError:
        body = ""
    lines = body.splitlines()
    excerpt = "\n".join(lines[:EXCERPT_LINES])
    cut = len(lines) > EXCERPT_LINES or len(excerpt) > EXCERPT_CHARS
    excerpt = excerpt[:EXCERPT_CHARS]
    out = [head]
    if m.subject:
        out.append(m.subject)
    if excerpt:
        out.append(excerpt)
    if cut:
        out.append(f"… [full: {store.body_path(rt, m.id)}]")
    if m.type == "question":
        out.append("QUESTION — your next Enter answers it (prefix ! to send an instruct instead)")
    return "\n".join(out)


# --- commands -------------------------------------------------------------------------

def is_command(text: str) -> bool:
    words = text.strip().split(maxsplit=1)
    return bool(words) and words[0] == "/ads"


def write_restart_request(runtime: Runtime | Path | str, target: str,
                          resume: bool = False) -> Path:
    """Drop `work/run/requests/<ts>-restart.json` for the supervisor and touch the poke file."""
    rt = _rt(runtime)
    rt.requests.mkdir(parents=True, exist_ok=True)
    path = rt.requests / f"{time.time_ns()}-restart.json"
    tmp = path.with_name("." + path.name + ".tmp")
    payload = {"op": "restart", "agent": target, "resume": resume, "from": HUMAN,
               "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    tmp.write_text(json.dumps(payload) + "\n")
    os.replace(tmp, path)
    ledger.touch_poke(rt)
    log.info("restart request %s -> %s", target, path.name)
    return path


def run_command(runtime: Runtime | Path | str, cfg: Any, text: str) -> str:
    """Execute a `/ads ...` command; return the text to print."""
    m = re.match(r"\s*(\S*)\s?(.*)", text.strip()[len("/ads"):], re.S)
    cmd, arg = (m.group(1), m.group(2)) if m else ("", "")
    if not cmd or cmd == "help":
        return HELP
    if cmd == "status":
        return render_status(runtime, cfg)
    if cmd == "inbox":
        return render_inbox(runtime)
    if cmd == "keys":
        return "key probe needs the terminal: run it inside the editor, or `ads doctor --key-probe`"
    if cmd == "instruct":
        if not arg.strip():
            return "usage: /ads instruct <text>"
        return send_input(runtime, cfg, arg, force_instruct=True)
    if cmd == "restart":
        words = arg.split()
        resume = "--resume" in words
        words = [w for w in words if w != "--resume"]
        if len(words) != 1 or words[0] not in (*AGENTS, "supervisor"):
            return "usage: /ads restart <" + "|".join(AGENTS) + "|supervisor> [--resume]"
        target = words[0]
        path = write_restart_request(runtime, target, resume)
        msg = f"restart requested: {target}{' --resume' if resume else ''} ({path.name})"
        if not supervisor_alive(runtime):
            msg += ("\nwarning: supervisor is not running; requests are processed by it — "
                    "run `ads restart supervisor` from a shell")
        return msg
    return f"unknown command: /ads {cmd}\n{HELP}"


# --- submit ---------------------------------------------------------------------------

def send_input(runtime: Runtime | Path | str, cfg: Any, text: str,
               force_instruct: bool = False) -> str:
    """Send `text` to the orchestrator as answer (open question) or instruct; return a status."""
    body = text.strip("\n").rstrip()
    if not body.strip():
        return ""
    subject = subject_of(body)
    q = None if force_instruct else open_question(runtime)
    try:
        if q is not None:
            msg = ledger.send(runtime, cfg, from_=HUMAN, to=ORCH, type="answer",
                              subject=subject, body=body + "\n", re=q["id"])
            status = f"answer {msg.id} → orchestrator (re {q['id']})"
        else:
            msg = ledger.send(runtime, cfg, from_=HUMAN, to=ORCH, type="instruct",
                              subject=subject, body=body + "\n")
            status = f"instruct {msg.id} → orchestrator" + (
                f" [{msg.status}]" if msg.status != "queued" else "")
    except (ledger.LedgerError, OSError, ValueError) as e:
        log.warning("send failed: %s", e)
        return f"error: {e}"
    log.info("sent %s", status)
    return status


def submit_text(runtime: Runtime | Path | str, cfg: Any, text: str) -> str:
    """Handle one submitted input: `/ads` command, `!` forced instruct, answer or instruct.

    Returns the status/output text ("" for blank input).
    """
    if not text.strip():
        return ""
    if is_command(text):
        return run_command(runtime, cfg, text)
    stripped = text.lstrip()
    if stripped.startswith("!"):
        return send_input(runtime, cfg, stripped[1:], force_instruct=True)
    return send_input(runtime, cfg, text)


# --- key probe ------------------------------------------------------------------------

def key_probe(seconds: float = 5, fd: int | None = None,
              out: Callable[[str], None] | None = None) -> int:
    """Put the tty in raw mode and print the repr of every byte chunk read for `seconds`."""
    fd = sys.stdin.fileno() if fd is None else fd
    emit = out or (lambda s: print(s, flush=True))
    if not os.isatty(fd):
        emit("key probe: stdin is not a terminal")
        return 1
    emit(f"key probe: press keys for {seconds:g} s (e.g. Shift+Enter, Alt+Enter, C-j) ...")
    old = termios.tcgetattr(fd)
    chunks: list[bytes] = []
    try:
        tty.setraw(fd)
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            r, _, _ = select.select([fd], [], [], left)
            if r:
                data = os.read(fd, 1024)
                if not data:
                    break
                chunks.append(data)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if not chunks:
        emit("key probe: no input")
    for c in chunks:
        emit(repr(c))
    return 0


# --- the app --------------------------------------------------------------------------

class Editor:
    """PromptSession-based pane-3 editor. Submitting never exits the prompt."""

    def __init__(self, runtime: Runtime | Path | str, cfg: Any, *, input: Any = None,
                 output: Any = None, tick_s: float = TICK_S) -> None:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.enums import EditingMode
        from prompt_toolkit.history import FileHistory

        self.rt = _rt(runtime)
        self.cfg = cfg
        self.tick_s = tick_s
        setup_logging(self.rt)
        hist = self.rt.root / cfg.editor.history_file
        hist.parent.mkdir(parents=True, exist_ok=True)
        self.printed: list[str] = []   # everything printed above the prompt (tests read it)
        self.status = "Enter: send · C-j/Alt+Enter: newline · /ads help"
        self._last_cc = 0.0
        self._toolbar = ["", ""]
        self._question: str | None = None
        self._last_seq = 0
        self._refresh(initial=True)
        self.session: PromptSession[str] = PromptSession(
            message=self._prompt_message,
            multiline=True,
            history=FileHistory(str(hist)),
            enable_history_search=True,
            editing_mode=EditingMode.VI if cfg.editor.vi_mode else EditingMode.EMACS,
            prompt_continuation=self._continuation,
            bottom_toolbar=self._bottom_toolbar,
            key_bindings=self._bindings(),
            input=input,
            output=output,
            erase_when_done=True,
        )
        self.app = self.session.app
        # Esc is a prefix of Esc-Enter (Alt+Enter = newline): a lone Esc is resolved after
        # `timeoutlen`. Short enough for "Esc, then Enter submits"; long enough for vi `gg`.
        self.app.ttimeoutlen = 0.05
        self.app.timeoutlen = ESC_TIMEOUT_S

    # -- rendering

    def _prompt_message(self) -> str:
        return "answer> " if self._question else "ads> "

    def _continuation(self, width: int, line_number: int, wrap_count: int) -> str:
        return "." * (width - 1) + " "

    def _bottom_toolbar(self) -> str:
        lines = [self._toolbar[0]]
        second = " | ".join(x for x in (self._toolbar[1], self.status) if x)
        lines.append(second)
        return "\n".join(lines)

    def _refresh(self, initial: bool = False) -> list[Message]:
        """Recompute toolbar cache; return new messages to the human since last refresh."""
        try:
            self._toolbar = toolbar_lines(self.rt)
            q = open_question(self.rt)
            self._question = q["id"] if q else None
            msgs = human_messages(self.rt)
        except Exception as e:  # never let a bad file kill the editor
            log.exception("refresh failed")
            self._toolbar = [f"toolbar error: {e}", ""]
            return []
        new = [m for m in msgs if m.seq > self._last_seq]
        if msgs:
            self._last_seq = max(self._last_seq, msgs[-1].seq)
        return [] if initial else new

    def emit(self, text: str) -> None:
        """Print text above the prompt."""
        self.printed.append(text)
        from prompt_toolkit.application import run_in_terminal

        def _p() -> None:
            print(text, flush=True)
        try:
            run_in_terminal(_p)
        except Exception:
            print(text, flush=True)

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)
            for m in self._refresh():
                log.info("incoming %s %s from %s", m.id, m.type, m.from_)
                self.emit(render_incoming(self.rt, m))
            self.app.invalidate()

    # -- submit

    def submit(self, text: str) -> None:
        if is_command(text) and text.strip().split()[1:2] == ["keys"]:
            self.emit("/ads keys")
            from prompt_toolkit.application import run_in_terminal
            run_in_terminal(lambda: key_probe(5))
            return
        result = submit_text(self.rt, self.cfg, text)
        if is_command(text):
            self.emit(f"{text.strip()}\n{result}")
            self.status = text.strip().split("\n")[0][:40]
        else:
            echo = "\n".join("│ " + ln for ln in text.strip("\n").splitlines())
            self.emit(f"{echo}\n→ {result}")
            self.status = result
        self._refresh()  # pick up the new question/answer state right away

    def _bindings(self) -> Any:
        from prompt_toolkit.filters import Condition, is_searching
        from prompt_toolkit.key_binding import KeyBindings

        kb = KeyBindings()

        @kb.add("enter", filter=~is_searching)
        def _enter(event: Any) -> None:
            buf = event.current_buffer
            text = buf.text
            if not text.strip():
                return
            buf.append_to_history()
            buf.reset()
            try:
                self.submit(text)
            except Exception as e:
                log.exception("submit failed")
                self.status = f"error: {e}"

        @kb.add("c-j", filter=~is_searching)
        @kb.add("escape", "enter", filter=~is_searching)
        def _newline(event: Any) -> None:
            event.current_buffer.newline(copy_margin=False)

        @kb.add("c-c", filter=~is_searching)
        def _cc(event: Any) -> None:
            now = time.monotonic()
            if now - self._last_cc <= EXIT_WINDOW_S:
                log.info("exit (C-c C-c)")
                event.app.exit(result="")
                return
            self._last_cc = now
            event.current_buffer.reset()
            self.status = "cleared (C-c again within 1 s exits)"

        @kb.add("c-d", filter=Condition(lambda: not self.app.current_buffer.text))
        def _cd(event: Any) -> None:
            self.status = "C-d ignored; C-c C-c exits"

        return kb

    # -- run

    def run(self) -> int:
        log.info("editor start (runtime %s)", self.rt.root)

        def _pre_run() -> None:
            self.app.create_background_task(self._ticker())
        try:
            self.session.prompt(pre_run=_pre_run)
        except (EOFError, KeyboardInterrupt):
            pass
        log.info("editor exit")
        return 0


def main(runtime: Runtime | Path | str, cfg: Any) -> int:
    """Entry point for `ads input`."""
    return Editor(runtime, cfg).run()
