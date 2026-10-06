"""`ads` command line. Keep module-level imports stdlib-light: `ads hook` runs on every Claude event."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from ads import __version__

SUBCOMMANDS = ("start", "stop", "attach", "status", "send", "note", "restart",
               "doctor", "hook", "supervisor", "input")

# Subcommand -> milestone that implements it (all implemented).
PENDING: dict[str, str] = {}


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--runtime", help="ads runtime dir (default: $ADS_RUNTIME or nearest ads.toml upward)")
    p.add_argument("--config", help="config file (default: <runtime>/ads.toml)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ads", description="Audiso Development System. `ads <project>` is short for `ads start <project>`.")
    parser.add_argument("--version", action="version", version=f"ads {__version__}")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    p = sub.add_parser("start", help="start (or attach to) the cell for a project")
    _common(p)
    p.add_argument("project")
    p.add_argument("--resume", action="store_true", help="resume agents' previous Claude sessions")
    p.add_argument("--no-attach", action="store_true")
    p.add_argument("--yes", "-y", action="store_true", help="create a missing project dir without asking")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--restart", action="store_true", help="kill a running session and start fresh")
    g.add_argument("--attach", action="store_true", help="attach to a running session")

    for name, help_ in (("stop", "stop the running cell"), ("attach", "attach to the running cell")):
        _common(sub.add_parser(name, help=help_))

    p = sub.add_parser("status", help="show agents, messages and tasks")
    _common(p)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("send", help="send a bus message")
    _common(p)
    p.add_argument("--from", dest="sender")
    p.add_argument("--to")
    p.add_argument("--type")
    p.add_argument("--re")
    p.add_argument("--parent")
    p.add_argument("--supersede")
    p.add_argument("--result")
    p.add_argument("--requeue")
    p.add_argument("--subject")
    body = p.add_mutually_exclusive_group()
    body.add_argument("--body")
    body.add_argument("--body-file")

    p = sub.add_parser("note", help="append a Lab Note to the runtime CLAUDE.md")
    _common(p)
    p.add_argument("text")

    p = sub.add_parser("restart", help="restart an agent or the supervisor")
    _common(p)
    p.add_argument("target", help="agent name or 'supervisor'")
    p.add_argument("--resume", action="store_true")

    p = sub.add_parser("doctor", help="check the environment")
    _common(p)
    p.add_argument("project", nargs="?", help="also check project/runtime overlap")
    p.add_argument("--key-probe", action="store_true", help="print raw bytes of the next key")

    p = sub.add_parser("hook", help="(internal) Claude Code hook handler")
    p.add_argument("event")
    _common(sub.add_parser("supervisor", help="(internal) delivery loop"))
    _common(sub.add_parser("input", help="(internal) human editor pane"))
    return parser


def normalize_argv(argv: Sequence[str]) -> list[str]:
    """`ads <project> ...` -> `ads start <project> ...`; leading --runtime/--config move after the subcommand."""
    argv = list(argv)
    lead: list[str] = []
    while argv and (argv[0] in ("--runtime", "--config") or argv[0].startswith(("--runtime=", "--config="))):
        lead.append(argv.pop(0))
        if "=" not in lead[-1] and argv:
            lead.append(argv.pop(0))
    if argv and argv[0] in SUBCOMMANDS:
        return [argv[0], *lead, *argv[1:]]
    if (argv or lead) and not (argv and argv[0] in ("-h", "--help", "--version")):
        return ["start", *lead, *argv]
    return argv


def main(argv: Sequence[str] | None = None) -> int:
    argv = normalize_argv(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 2
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] | None = HANDLERS.get(args.cmd)
    if handler is None:
        print(f"ads {args.cmd}: not implemented yet (milestone {PENDING.get(args.cmd, '?')})",
              file=sys.stderr)
        return 2
    return handler(args)


# --- doctor --------------------------------------------------------------------

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
Row = tuple[str, str, str]  # (status, check, detail)


def _run(argv: list[str], timeout: float = 20) -> tuple[int, str]:
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:g}s"
    return cp.returncode, (cp.stdout + cp.stderr).strip()


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text else ""


def _check_tmux() -> Row:
    code, out = _run(["tmux", "-V"])
    if code != 0:
        return FAIL, "tmux >= 3.2", out or f"exit {code}"
    m = re.search(r"(\d+)\.(\d+)", out)
    if not m:
        return WARN, "tmux >= 3.2", f"cannot parse version: {out}"
    ok = (int(m[1]), int(m[2])) >= (3, 2)
    return (PASS if ok else FAIL), "tmux >= 3.2", out


def _check_claude(claude_bin: str) -> list[Row]:
    exe = os.environ.get("ADS_CLAUDE_BIN") or claude_bin
    code, out = _run([exe, "--version"])
    if code != 0:
        return [(FAIL, "claude --version", f"{exe}: {_first_line(out) or f'exit {code}'}")]
    rows: list[Row] = [(PASS, "claude --version", f"{_first_line(out)} ({shutil.which(exe) or exe})")]
    code, out = _run([exe, "auth", "status"])
    rows.append((PASS if code == 0 else WARN, "claude auth status",
                 "logged in" if code == 0 else f"exit {code}: {_first_line(out)}"))
    return rows


def _check_python() -> list[Row]:
    v = sys.version_info
    rows: list[Row] = [((PASS if v >= (3, 14) else FAIL), "python >= 3.14",
                        f"{v.major}.{v.minor}.{v.micro} ({sys.executable})")]
    try:
        import prompt_toolkit
        rows.append((PASS, "prompt_toolkit", prompt_toolkit.__version__))
    except ImportError as e:
        rows.append((FAIL, "prompt_toolkit", str(e)))
    ads_bin = Path(sys.executable).parent / "ads"
    rows.append((PASS if os.access(ads_bin, os.X_OK) else WARN, "hook binary", str(ads_bin)))
    return rows


def _check_ads_server(socket: str) -> Row | None:
    """V3: a running ads tmux server must have `extended-keys on` (Shift+Enter)."""
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    try:
        cp = subprocess.run(["tmux", "-L", socket, "show-options", "-s", "-v", "extended-keys"],
                            capture_output=True, text=True, timeout=5, env=env)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:  # no server on the socket
        return None
    value = cp.stdout.strip()
    if value in ("on", "always"):
        return PASS, "ads tmux server", f"-L {socket}: extended-keys {value}"
    return (WARN, "ads tmux server",
            f"-L {socket}: extended-keys is {value or 'unset'} (Shift+Enter won't reach the "
            "editor; the server was not started with ads.tmux.conf? `ads stop` and restart)")


def doctor_rows(runtime: Path | None, cfg, project: str | Path | None = None) -> list[Row]:
    """Environment checks shared by `ads doctor` and the `ads start` preflight."""
    from ads.paths import OverlapError, check_overlap
    rows: list[Row] = [_check_tmux()]
    rows.extend(_check_claude(cfg.ads.claude_bin))
    rows.extend(_check_python())
    term = os.environ.get("TERM", "")
    rows.append((WARN if term in ("", "dumb") else PASS, "$TERM", term or "(unset)"))
    if os.environ.get("TMUX"):
        rows.append((WARN, "nested tmux",
                     "outer tmux needs `set -s extended-keys on` and "
                     "`set -as terminal-features 'xterm*:extkeys'` for Shift+Enter; ads prefix "
                     f"is {cfg.ads.tmux_prefix} (outer keeps C-b)"))
    server = _check_ads_server(cfg.ads.tmux_socket)
    if server:
        rows.append(server)
    if project:
        if runtime is None:
            rows.append((FAIL, "project overlap", "runtime unknown"))
        else:
            try:
                check_overlap(project, runtime)
                rows.append((PASS, "project overlap", str(Path(project).expanduser().resolve())))
            except OverlapError as e:
                rows.append((FAIL, "project overlap", str(e)))
    return rows


def cmd_doctor(args: argparse.Namespace) -> int:
    if args.key_probe:
        from ads.editor import key_probe
        return key_probe(5)
    from ads.config import ConfigError, default_config, load_config, resolve_runtime

    rows: list[Row] = []
    cfg = default_config()
    runtime: Path | None = None
    try:
        runtime = resolve_runtime(args.runtime)
        rows.append((PASS, "runtime", str(runtime)))
    except ConfigError as e:
        rows.append((FAIL, "runtime", str(e)))
    try:
        cfg = load_config(args.config, runtime)
        rows.append((PASS, "config", str(cfg.source or "built-in defaults")))
    except ConfigError as e:
        rows.append((FAIL, "config", str(e)))
    rows.extend(doctor_rows(runtime, cfg, args.project))

    width = max(len(r[1]) for r in rows)
    for status, check, detail in rows:
        print(f"{status}  {check:<{width}}  {detail}")
    fails = sum(r[0] == FAIL for r in rows)
    warns = sum(r[0] == WARN for r in rows)
    print(f"\n{len(rows)} checks: {len(rows) - fails - warns} pass, {warns} warn, {fails} fail")
    return 1 if fails else 0


# --- hook ----------------------------------------------------------------------------

def cmd_hook(args: argparse.Namespace) -> int:
    """Claude Code hook: stdlib + ads.bus only (see ads.hooks); always exit 0."""
    try:
        from ads.hooks import main as hook_main
    except BaseException:  # a hook must never fail the Claude session
        return 0
    return hook_main(args.event)


# --- shared helpers --------------------------------------------------------------------

class CliError(Exception):
    """User-facing error: printed to stderr, exit 1."""


def _runtime_cfg(args: argparse.Namespace, need_cfg: bool = True):  # -> (Runtime, Config | None)
    from ads.config import ConfigError, load_config, resolve_runtime
    from ads.paths import Runtime
    try:
        root = resolve_runtime(getattr(args, "runtime", None))
        cfg = load_config(getattr(args, "config", None), root) if need_cfg else None
    except ConfigError as e:
        raise CliError(str(e)) from None
    return Runtime(root), cfg


def _guard(fn: Callable[[argparse.Namespace], int]) -> Callable[[argparse.Namespace], int]:
    def wrapper(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except CliError as e:
            print(f"ads {args.cmd}: {e}", file=sys.stderr)
            return 1
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# --- send ------------------------------------------------------------------------------

def _read_body(args: argparse.Namespace) -> str:
    if args.body is not None:
        return sys.stdin.read() if args.body == "-" else args.body
    if args.body_file is not None:
        if args.body_file == "-":
            return sys.stdin.read()
        try:
            return Path(args.body_file).expanduser().read_text(encoding="utf-8")
        except OSError as e:
            raise CliError(f"--body-file: {e}") from None
    raise CliError("a body is required: --body TEXT, --body-file FILE, or --body - (stdin)")


def _requeue(args: argparse.Namespace) -> int:
    from ads.bus import ledger, store
    rt, _ = _runtime_cfg(args, need_cfg=False)
    try:
        with ledger.ledger_lock(rt):
            m = store.get(rt, args.requeue)
            if m.status != "failed":
                raise CliError(f"--requeue {m.id}: status is {m.status}, only failed messages "
                               "can be requeued")
            store.update(rt, m.id, status="queued", enters=0, pastes=0, unconfirmed=False,
                         log_extra={"requeue": True})
    except store.MessageNotFound:
        raise CliError(f"--requeue {args.requeue}: no such message") from None
    ledger.touch_poke(rt)
    print(f"{m.id} requeued")
    return 0


@_guard
def cmd_send(args: argparse.Namespace) -> int:
    """`ads send`: validate and write a bus message via the ledger; print its id."""
    if args.requeue:
        return _requeue(args)
    sender = args.sender or os.environ.get("ADS_AGENT")
    missing = [f for f, v in (("--from (or $ADS_AGENT)", sender), ("--to", args.to),
                              ("--type", args.type), ("--subject", args.subject)) if not v]
    if missing:
        raise CliError("missing " + ", ".join(missing))
    body = _read_body(args)
    from ads.bus import ledger
    rt, cfg = _runtime_cfg(args)
    try:
        msg = ledger.send(rt, cfg, from_=sender, to=args.to, type=args.type, subject=args.subject,
                          body=body, re=args.re, parent=args.parent, supersede=args.supersede,
                          result=args.result)
    except ledger.LedgerError as e:
        raise CliError(str(e)) from None
    if msg.status == "held":
        print(f"{msg.id} held behind {msg.held_by}")
    elif msg.status == "ignored":
        print(f"{msg.id} ignored (late reply: task {args.re} is no longer open)")
    else:
        print(msg.id)
    return 0


# --- status ----------------------------------------------------------------------------

def _supervisor_info(rt) -> dict:
    info: dict = {"pid": None, "alive": False, "pid_file": str(rt.supervisor_pid)}
    try:
        pid = int(rt.supervisor_pid.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return info
    info["pid"] = pid
    try:
        os.kill(pid, 0)
        info["alive"] = True
    except PermissionError:
        info["alive"] = True
    except OSError:
        pass
    return info


def _recent_alerts(rt, n: int = 5) -> list[dict]:
    import json
    out = []
    for p in sorted(rt.alerts.glob("*.json"))[-n:]:
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            d = {}
        out.append({"file": p.name, **(d if isinstance(d, dict) else {})})
    return out


def status_data(rt, cfg) -> dict:
    """Everything `ads status` shows, as a JSON-serializable dict."""
    from ads.bus import ledger, store
    from ads.bus.state import all_states
    from ads.paths import AGENTS
    msgs = store.all_messages(rt)
    agents: dict = {}
    for name, st in all_states(rt).items():
        inflight = st.get("inflight_msg") or next(
            (m.id for m in msgs if m.to == name and m.status == "delivering"), None)
        agents[name] = {
            "state": st.get("state"), "reason": st.get("reason"), "since": st.get("since"),
            "model": cfg.agents[name].model if name in cfg.agents else None,
            "inflight": inflight,
            "queued": sum(m.to == name and m.status == "queued" for m in msgs),
            "held": sum(m.to == name and m.status == "held" for m in msgs),
            "progress_this_turn": st.get("progress_this_turn", 0),
            "last_error": st.get("last_error"),
        }
    tasks = [{k: t.get(k) for k in ("id", "from", "to", "type", "state", "nudges", "subject",
                                     "parent", "created")}
             for t in ledger.open_tasks(rt)]
    pending = [m for m in msgs if m.status in ("queued", "held", "delivering")]
    return {
        "runtime": str(rt.root),
        "agents": agents,
        "tasks": tasks,
        "messages": [{"id": m.id, "from": m.from_, "to": m.to, "type": m.type,
                      "status": m.status, "held_by": m.held_by, "subject": m.subject}
                     for m in pending],
        "supersede_pending": [{"task": m.supersedes, "replacement": m.id, "to": m.to,
                               "status": m.status}
                              for m in pending if m.supersedes],
        "phase": ledger.current_phase(rt),
        "supervisor": _supervisor_info(rt),
        "alerts": _recent_alerts(rt),
        "agent_order": list(AGENTS),
    }


def _print_status(d: dict) -> None:
    sup = d["supervisor"]
    sup_txt = (f"alive (pid {sup['pid']})" if sup["alive"]
               else f"not running (stale pid {sup['pid']})" if sup["pid"] else "not running")
    print(f"runtime: {d['runtime']}   phase: {d['phase'] or '-'}   supervisor: {sup_txt}")
    print()
    rows = [("AGENT", "STATE", "REASON", "SINCE", "MODEL", "INFLIGHT", "Q", "H")]
    for name, a in d["agents"].items():
        rows.append((name, a["state"] or "-", a["reason"] or "-", a["since"] or "-",
                     a["model"] or "-", a["inflight"] or "-", str(a["queued"]), str(a["held"])))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    print()
    print(f"open tasks ({len(d['tasks'])}):")
    for t in d["tasks"]:
        print(f"  {t['id']}  {t['from']}->{t['to']}  {t['type']:<14} {t['state']:<9} "
              f"nudges={t['nudges']}  {t['subject']}")
    held = [m for m in d["messages"] if m["status"] == "held"]
    if held:
        print(f"held ({len(held)}):")
        for m in held:
            print(f"  {m['id']}  {m['from']}->{m['to']}  {m['type']}  held by {m['held_by']}")
    for s in d["supersede_pending"]:
        print(f"supersede pending: {s['replacement']} -> {s['to']} supersedes {s['task']} "
              f"({s['status']})")
    if d["alerts"]:
        print("alerts:")
        for a in d["alerts"]:
            print(f"  {a['file']}: {a.get('agent')} {a.get('error_type')}: {a.get('error_message')}")


@_guard
def cmd_status(args: argparse.Namespace) -> int:
    """`ads status [--json]`."""
    import json
    rt, cfg = _runtime_cfg(args)
    data = status_data(rt, cfg)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        _print_status(data)
    return 0


# --- note ------------------------------------------------------------------------------

LAB_NOTES = "## Lab Notes"
NOTES_WARN = 80


def add_lab_note(claude_md: Path, lock_path: Path, author: str, text: str,
                 day: str | None = None) -> int:
    """Append `- [day author] text` at the end of `## Lab Notes` (created if missing).

    Runs under flock on lock_path and replaces the file atomically. Returns the entry count.
    """
    import fcntl
    import tempfile
    from datetime import date
    entry = f"- [{day or date.today().isoformat()} {author}] {' '.join(text.split())}"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                lines = claude_md.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []
            try:
                start = next(i for i, ln in enumerate(lines) if ln.strip() == LAB_NOTES)
            except StopIteration:
                if lines and lines[-1].strip():
                    lines.append("")
                lines.append(LAB_NOTES)
                start = len(lines) - 1
            end = next((i for i in range(start + 1, len(lines))
                        if lines[i].startswith(("# ", "## "))), len(lines))
            insert = end
            while insert > start + 1 and not lines[insert - 1].strip():
                insert -= 1
            lines.insert(insert, entry)
            count = sum(ln.startswith("- [") for ln in lines[start + 1:end + 1])
            fd, tmp = tempfile.mkstemp(dir=claude_md.parent, prefix=f".{claude_md.name}.",
                                       suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                if claude_md.exists():
                    os.chmod(tmp, claude_md.stat().st_mode & 0o7777)
                else:
                    os.chmod(tmp, 0o644)
                os.replace(tmp, claude_md)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return count


@_guard
def cmd_note(args: argparse.Namespace) -> int:
    """`ads note "text"` -> Lab Notes in <runtime>/CLAUDE.md."""
    if not args.text.strip():
        raise CliError("empty note")
    rt, _ = _runtime_cfg(args, need_cfg=False)
    author = os.environ.get("ADS_AGENT") or "human"
    count = add_lab_note(rt.claude_md, rt.run / "claude-md.lock", author, args.text)
    print(f"noted in {rt.claude_md} ({count} entries)")
    if count > NOTES_WARN:
        print(f"ads note: warning: Lab Notes has {count} entries (> {NOTES_WARN}); "
              "consider condensing it", file=sys.stderr)
    return 0


# --- input (pane-3 editor) -------------------------------------------------------------

@_guard
def cmd_input(args: argparse.Namespace) -> int:
    """`ads input`: the human editor pane (ads.editor, M4)."""
    rt, cfg = _runtime_cfg(args)
    try:
        from ads.editor import main as editor_main
    except ImportError as e:
        raise CliError(f"the editor is unavailable ({e}); is prompt_toolkit installed and "
                       "ads.editor present?") from None
    return int(editor_main(rt, cfg) or 0)


# --- supervisor / restart ------------------------------------------------------------------

@_guard
def cmd_supervisor(args: argparse.Namespace) -> int:
    """`ads supervisor`: the delivery loop (runs in tmux window 2; single instance)."""
    rt, cfg = _runtime_cfg(args)
    from ads.supervisor import main as supervisor_main
    return supervisor_main(rt, cfg)


@_guard
def cmd_restart(args: argparse.Namespace) -> int:
    """`ads restart <agent>|human|supervisor [--resume]`."""
    from ads.paths import AGENTS, HUMAN
    from ads.supervisor import SupervisorError, respawn_supervisor, write_request
    rt, _ = _runtime_cfg(args, need_cfg=False)
    target = args.target
    if target == "supervisor":
        try:
            pane = respawn_supervisor(rt)
        except SupervisorError as e:
            raise CliError(str(e)) from None
        print(f"supervisor respawned in pane {pane}")
        return 0
    if target not in (*AGENTS, HUMAN):
        raise CliError(f"unknown target {target!r}; expected one of "
                       f"{', '.join([*AGENTS, HUMAN, 'supervisor'])}")
    path = write_request(rt, "restart", agent=target, resume=bool(args.resume), **{"from": "cli"})
    print(f"restart requested: {target}{' --resume' if args.resume else ''} ({path.name})")
    if not _supervisor_info(rt)["alive"]:
        print("ads restart: warning: the supervisor is not running; run `ads restart supervisor`",
              file=sys.stderr)
    return 0


# --- start / stop / attach (M5) ---------------------------------------------------------------

def _start_guard(fn):
    def wrapper(args: argparse.Namespace) -> int:
        from ads.start import StartError
        try:
            return fn(args)
        except StartError as e:
            raise CliError(str(e)) from None
    wrapper.__name__ = fn.__name__
    return _guard(wrapper)


@_start_guard
def cmd_start(args: argparse.Namespace) -> int:
    """`ads start <project>` / `ads <project>` (plan §10)."""
    from ads.start import start
    rt, cfg = _runtime_cfg(args)
    return start(rt, cfg, args.project, resume=args.resume, no_attach=args.no_attach,
                 attach_flag=args.attach, restart_flag=args.restart, yes=args.yes,
                 config_path=args.config)


@_start_guard
def cmd_stop(args: argparse.Namespace) -> int:
    """`ads stop`: stop the cell recorded in work/run/session.json."""
    from ads.start import stop
    rt, _ = _runtime_cfg(args, need_cfg=False)
    return stop(rt)


@_start_guard
def cmd_attach(args: argparse.Namespace) -> int:
    """`ads attach`: attach to the cell recorded in work/run/session.json."""
    from ads.start import cmd_attach as attach
    rt, cfg = _runtime_cfg(args)
    return attach(rt, cfg)


HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "start": cmd_start, "stop": cmd_stop, "attach": cmd_attach,
    "doctor": cmd_doctor, "hook": cmd_hook, "send": cmd_send, "status": cmd_status,
    "note": cmd_note, "input": cmd_input, "supervisor": cmd_supervisor, "restart": cmd_restart,
}


if __name__ == "__main__":
    sys.exit(main())
