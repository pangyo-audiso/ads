#!/usr/bin/env python3
"""Stand-in for `claude` in tmux tests (ADS_CLAUDE_BIN=tests/fake_agent.py). Stdlib only.

Accepts and ignores claude's argv (except --session-id/--resume <uuid>). Draws a minimal
screen shaped like Claude Code 2.1.289 (docs/spike-claude.md):

    <transcript>                       echoes use "❯ " + ASCII space
    ✶ Faking…                          spinner line, only while busy
    ──────────────── ads-<agent> ─
    ❯\\xa0<input>                       input box: ❯ + NO-BREAK SPACE
    ────────────────────────────────
      ⏵⏵ bypass permissions on (shift+tab to cycle)[ · esc to interrupt] · ← for agents

Bracketed paste is enabled; pasted text goes into the input line. Enter submits:
`$ADS_BIN hook prompt-submit` → busy for FAKE_BUSY_S (Enter ignored) → `hook stop`
(a block decision means: stay busy another FAKE_BUSY_S and call stop again). At start
(after the optional dialogs) it calls `hook session-start`.

Knobs: environment `FAKE_<NAME>`, overridden by `$ADS_RUNTIME/fake.json`, overridden by the
project's `$ADS_STATE_DIR/fake.json` ({"*": {...}, "<agent>": {...}}, keys lower-case without
the FAKE_ prefix):
  DIALOG           "trust", "bypass" or "trust,bypass": render the fixture; needs Down then
                   Enter (Enter on "No, exit" exits 1). An entry containing "/" is a file
                   rendered as-is (an "unknown" dialog; Down then Enter also dismisses it).
  SESSION_START_FIRST  1: call session-start BEFORE showing the dialogs (an idle agent with a
                   dialog on screen: the supervisor's watchdog must stay gated off)
  BUSY_S           busy time per turn (default 1)
  EXIT_AFTER       n: after the n-th prompt's busy time, exit 1 without a Stop hook (crash)
  REPLY            1: at the end of the busy time of a pointer to a task, `ads send` the
                   matching reply (report success / review pass / answer) to its sender
  DELEGATE         "<agent>[:<type>]" (with REPLY=1): instead of replying to a task, send a
                   child task (default type instruct) to <agent> with --parent; when the
                   child's reply arrives as a pointer, reply to the original task
  SWALLOW_ENTER    n: ignore the first n submitting Enters of this process (1 = first only)
  SKIP_HOOKS       comma list of hook events never called (e.g. "prompt-submit")
  SESSION_START_DELAY  seconds before the session-start hook (default 0)
  TRANSCRIPT       file whose lines pre-fill the transcript (e.g. a resumed session)
  LOG              file: JSON lines of everything that happened (keys, prompts, hooks)
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "dialogs"
AGENT = os.environ.get("ADS_AGENT", "fake")
RUNTIME = os.environ.get("ADS_RUNTIME")
STATE_DIR = os.environ.get("ADS_STATE_DIR") or RUNTIME  # <runtime>/projects/<name>
POINTER_RE = re.compile(r"^\[ADS-MSG id=(m-\d{8}-\d{6,}) from=([\w-]+) type=([\w-]+)\]")
REPLY = {"instruct": ("report", "success"), "review-request": ("review", "pass"),
         "question": ("answer", None)}
SPINNER = "*·✢✶✽"


def _knobs() -> dict[str, str]:
    knobs = {k[5:].lower(): v for k, v in os.environ.items() if k.startswith("FAKE_")}
    for base in dict.fromkeys(d for d in (RUNTIME, STATE_DIR) if d):
        try:
            data = json.loads((Path(base) / "fake.json").read_text())
            for key in ("*", AGENT):
                knobs.update({k.lower(): str(v) for k, v in (data.get(key) or {}).items()})
        except (OSError, ValueError, AttributeError):
            pass
    return knobs


K = _knobs()
BUSY_S = float(K.get("busy_s", "1"))
EXIT_AFTER = int(K.get("exit_after", "0") or 0)
DO_REPLY = K.get("reply", "0") == "1"
DELEGATE = K.get("delegate", "")
SWALLOW = int(K.get("swallow_enter", "0") or 0)
SKIP_HOOKS = {h for h in K.get("skip_hooks", "").split(",") if h}
DIALOGS = [d for d in K.get("dialog", "").split(",") if d]
LOG = K.get("log")


def _session_id(argv: list[str]) -> tuple[str, str]:
    for flag, source in (("--resume", "resume"), ("--session-id", "startup")):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                return argv[i + 1], source
    return "fake-session", "startup"


SESSION_ID, SOURCE = _session_id(sys.argv[1:])


def log(event: str, **kw) -> None:
    if not LOG:
        return
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.time(), "agent": AGENT, "event": event, **kw},
                           ensure_ascii=False) + "\n")


class Screen:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.transcript: list[str] = []
        self.input = ""
        self.busy = False
        self.dialog: str | None = None
        self.selected = 0
        self.prompts = 0
        self.swallowed = 0
        self.frame = 0
        self.children: dict[str, str] = {}  # delegated child task id -> original task id
        tfile = K.get("transcript")
        if tfile:
            try:
                self.transcript = Path(tfile).read_text(encoding="utf-8").splitlines()
            except OSError:
                pass

    # --- drawing --------------------------------------------------------------------
    def _size(self) -> tuple[int, int]:
        sz = shutil.get_terminal_size((100, 30))
        return max(sz.columns, 20), max(sz.lines, 10)

    def _dialog_lines(self) -> list[str]:
        if "/" in self.dialog:
            path = Path(self.dialog)
        else:
            path = FIXTURES / f"{self.dialog}{'_selected' if self.selected else ''}.txt"
        lines = path.read_text(encoding="utf-8").splitlines()
        while lines and not lines[-1].strip():
            lines.pop()
        while lines and not lines[0].strip():
            lines.pop(0)
        return lines

    def draw(self) -> None:
        with self.lock:
            cols, rows = self._size()
            if self.dialog:
                body = self._dialog_lines()
            else:
                bottom = []
                if self.busy:
                    self.frame += 1
                    bottom.append(f"{SPINNER[self.frame % len(SPINNER)]} Faking… (1s)")
                bottom.append("")
                name = f" ads-{AGENT} ─"
                bottom.append("─" * max(cols - 1 - len(name), 4) + name)
                bottom.append("❯\xa0" + self.input)
                bottom.append("─" * (cols - 1))
                status = "  ⏵⏵ bypass permissions on (shift+tab to cycle)"
                if self.busy:
                    status += " · esc to interrupt"
                bottom.append(status + " · ← for agents")
                room = rows - len(bottom) - 1
                body = [f"fake claude ({AGENT})", ""] + self.transcript
                body = body[-room:] if room > 0 else []
                body = body + bottom
            body = body[-(rows - 1):]
            out = "\x1b[H\x1b[2J" + "\r\n".join(line[: cols - 1] for line in body)
            os.write(1, out.encode("utf-8"))

    # --- hooks ----------------------------------------------------------------------
    def hook(self, event: str, payload: dict) -> str:
        ads_bin = os.environ.get("ADS_BIN")
        base = {"session_id": SESSION_ID, "cwd": os.getcwd(), "session_title": f"ads-{AGENT}"}
        if not ads_bin or event in SKIP_HOOKS:
            log("hook-skipped", hook=event)
            return ""
        try:
            cp = subprocess.run([ads_bin, "hook", event], input=json.dumps({**base, **payload}),
                                capture_output=True, text=True, timeout=30)
            out = cp.stdout
        except (OSError, subprocess.TimeoutExpired) as e:
            out = ""
            log("hook-error", hook=event, error=str(e))
        log("hook", hook=event, stdout=out.strip()[:500])
        return out

    def session_start(self) -> None:
        delay = float(K.get("session_start_delay", "0") or 0)
        if delay:
            time.sleep(delay)
        self.hook("session-start", {"hook_event_name": "SessionStart", "source": SOURCE,
                                    "model": "fake"})

    # --- turn -----------------------------------------------------------------------
    def submit(self) -> None:
        text = self.input
        if not text.strip():
            return
        if self.swallowed < SWALLOW:
            self.swallowed += 1
            log("enter-swallowed", text=text)
            return
        self.input = ""
        self.busy = True
        self.prompts += 1
        self.transcript.append("❯ " + text)
        self.transcript.append("")
        log("submit", text=text, n=self.prompts)
        threading.Thread(target=self.turn, args=(text, self.prompts), daemon=True).start()

    def _sleep_busy(self) -> None:
        end = time.time() + BUSY_S
        while time.time() < end:
            time.sleep(min(0.2, max(end - time.time(), 0)))
            self.draw()

    def turn(self, text: str, n: int) -> None:
        self.hook("prompt-submit", {"hook_event_name": "UserPromptSubmit", "prompt": text,
                                    "permission_mode": "bypassPermissions"})
        self.draw()
        self._sleep_busy()
        m = POINTER_RE.match(text)
        if m and DO_REPLY:
            self.handle(m.group(1))
        if EXIT_AFTER and n >= EXIT_AFTER:
            log("exit", reason="exit_after", n=n)
            os._exit(1)
        active = False
        while True:
            out = self.hook("stop", {"hook_event_name": "Stop", "stop_hook_active": active,
                                     "last_assistant_message": "ok"})
            decision = None
            try:
                decision = json.loads(out).get("decision") if out.strip() else None
            except ValueError:
                pass
            if decision != "block":
                break
            with self.lock:
                self.transcript.append("● (stop hook blocked; continuing)")
            active = True
            self._sleep_busy()
        with self.lock:
            self.transcript.append("● ok")
            self.transcript.append(f"✻ Faked for {BUSY_S:g}s")
            self.transcript.append("")
            self.busy = False
        self.draw()

    def _envelope(self, msg_id: str) -> dict | None:
        try:
            return json.loads((Path(STATE_DIR) / "work" / "msgs" / f"{msg_id}.json").read_text())
        except (OSError, ValueError, TypeError) as e:
            log("reply-error", error=str(e))
            return None

    def handle(self, msg_id: str) -> None:
        """REPLY=1: reply to a task, delegate it (DELEGATE), or finish a delegated task."""
        env = self._envelope(msg_id)
        if env is None:
            return
        if env.get("type") in REPLY and DELEGATE:
            target, _, ctype = DELEGATE.partition(":")
            argv = [os.environ.get("ADS_BIN", "ads"), "send", "--to", target,
                    "--type", ctype or "instruct", "--parent", msg_id,
                    "--subject", f"Delegated: {env.get('subject', '')}",
                    "--body", f"fake delegation from {AGENT}"]
            cp = subprocess.run(argv, capture_output=True, text=True)
            out = (cp.stdout + cp.stderr).strip()
            log("delegate", msg_id=msg_id, rc=cp.returncode, out=out)
            if cp.returncode == 0 and out:
                self.children[out.split()[0]] = msg_id
        elif env.get("type") in REPLY:
            self.reply(msg_id)
        elif env.get("re") in self.children:
            self.reply(self.children.pop(env["re"]))

    def reply(self, msg_id: str) -> None:
        env = self._envelope(msg_id)
        if env is None:
            return
        kind = REPLY.get(env.get("type"))
        if not kind:
            return
        rtype, result = kind
        argv = [os.environ.get("ADS_BIN", "ads"), "send", "--to", env["from"], "--type", rtype,
                "--re", msg_id, "--subject", f"Re: {env.get('subject', '')}",
                "--body", f"fake {rtype} from {AGENT}"]
        if result:
            argv += ["--result", result]
        cp = subprocess.run(argv, capture_output=True, text=True)
        log("reply", msg_id=msg_id, rc=cp.returncode, out=(cp.stdout + cp.stderr).strip())

    # --- keys -----------------------------------------------------------------------
    def key(self, name: str, text: str = "") -> None:
        log("key", key=name, **({"text": text} if text else {}))
        with self.lock:
            if self.dialog:
                if name in ("down", "up"):
                    self.selected = 1 if name == "down" else 0
                elif name == "enter":
                    if not self.selected:
                        log("exit", reason=f"{self.dialog}: No, exit")
                        os.write(1, b"\x1b[?2004l\r\n")
                        os._exit(1)
                    log("dialog-accepted", dialog=self.dialog)
                    DIALOGS.pop(0)
                    self.dialog = DIALOGS[0] if DIALOGS else None
                    self.selected = 0
                    if self.dialog is None and K.get("session_start_first") != "1":
                        threading.Thread(target=self.session_start, daemon=True).start()
                self.draw()
                return
            if name == "paste":
                self.input += text.replace("\r", " ").replace("\n", " ")
            elif name == "char":
                self.input += text
            elif name == "backspace":
                self.input = self.input[:-1]
            elif name == "ctrl-u" or name == "ctrl-c":
                self.input = ""
            elif name == "enter":
                if self.busy:
                    log("enter-ignored-busy")
                else:
                    self.submit()
            self.draw()


PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"


def parse(buf: bytes, screen: Screen) -> bytes:
    """Consume complete key sequences from buf; return the unconsumed rest."""
    while buf:
        if buf.startswith(PASTE_START):
            end = buf.find(PASTE_END)
            if end < 0:
                return buf
            screen.key("paste", buf[len(PASTE_START):end].decode("utf-8", "replace"))
            buf = buf[end + len(PASTE_END):]
        elif buf[:1] == b"\x1b":
            if PASTE_START.startswith(buf) or buf == b"\x1bO":
                return buf  # incomplete sequence: wait for more bytes
            seq = buf[:3]
            if seq in (b"\x1b[B", b"\x1bOB"):
                screen.key("down")
            elif seq in (b"\x1b[A", b"\x1bOA"):
                screen.key("up")
            else:
                m = re.match(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b.", buf)
                n = m.end() if m else 1
                screen.key("esc", buf[:n].decode("latin-1"))
                buf = buf[n:]
                continue
            buf = buf[3:]
        else:
            b = buf[0]
            if b in (13, 10):
                screen.key("enter")
                buf = buf[1:]
            elif b in (127, 8):
                screen.key("backspace")
                buf = buf[1:]
            elif b == 21:
                screen.key("ctrl-u")
                buf = buf[1:]
            elif b == 3:
                screen.key("ctrl-c")
                buf = buf[1:]
            elif b < 32:
                buf = buf[1:]
            else:
                # one UTF-8 character
                n = 1 if b < 0x80 else 2 if b < 0xE0 else 3 if b < 0xF0 else 4
                if len(buf) < n:
                    return buf
                screen.key("char", buf[:n].decode("utf-8", "replace"))
                buf = buf[n:]
    return buf


def main() -> int:
    if sys.argv[1:] == ["--version"]:  # `ads doctor` preflight
        print("2.1.289 (Claude Code) [fake_agent]")
        return 0
    if sys.argv[1:3] == ["auth", "status"]:
        print("fake_agent: logged in")
        return 0
    screen = Screen()
    log("start", argv=sys.argv[1:], dialogs=list(DIALOGS))
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd) if os.isatty(fd) else None
    if old:
        tty.setraw(fd)
    os.write(1, b"\x1b[?2004h")
    try:
        if DIALOGS and K.get("session_start_first") == "1":
            screen.draw()
            screen.session_start()  # synchronously: the agent is idle before the dialog shows
            time.sleep(0.3)
        if DIALOGS:
            screen.dialog = DIALOGS[0]
        else:
            threading.Thread(target=screen.session_start, daemon=True).start()
        screen.draw()
        buf = b""
        while True:
            r, _, _ = select.select([fd], [], [], 0.5)
            if not r:
                if buf:  # a lone Escape (or a stale partial sequence)
                    screen.key("esc", buf.decode("latin-1"))
                    buf = b""
                continue
            data = os.read(fd, 4096)
            if not data:
                return 0
            buf = parse(buf + data, screen)
    finally:
        os.write(1, b"\x1b[?2004l")
        if old:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    sys.exit(main())
