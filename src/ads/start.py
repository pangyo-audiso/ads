"""`ads start|stop|attach|list` (plan §10): project setup, preflight, layout, readiness, attach, stop.

Every project gets its own state dir `<runtime>/projects/<name>/` (ads.projects) and its own
tmux server `<[ads] tmux_socket>-<name>`, so cells of different projects run side by side.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from ads import launcher, projects
from ads.bus import state as agent_state
from ads.config import Config
from ads.paths import AGENTS, LAYOUT, HUMAN, ProjectState, session_name, socket_name
from ads.tmux import Tmux, TmuxError, build_layout, runtime_conf

SUPERVISOR_WAIT_S = 15.0
STOP_WAIT_S = 10.0
POLL_S = 0.25
LAB_NOTES = "## Lab Notes"
RUNTIME_GITIGNORE = ("projects/", ".venv/")

Out = Callable[[str], None]


class StartError(RuntimeError):
    """User-facing failure of start/stop/attach (printed, exit 1)."""


def _print(s: str) -> None:
    print(s, flush=True)


def _isatty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def ask(question: str) -> str:
    """Read one answer from the terminal ('' on EOF / non-tty)."""
    if not _isatty():
        return ""
    try:
        return input(question).strip().lower()
    except EOFError:
        return ""


# --- project / runtime setup ------------------------------------------------------------

def ensure_project(project: Path, cfg: Config, *, yes: bool, out: Out = _print,
                   asker: Callable[[str], str] = ask) -> bool:
    """Create a missing project (after asking unless --yes). False = user declined."""
    if project.exists() and not project.is_dir():
        raise StartError(f"{project} exists and is not a directory")
    if not project.exists():
        if not yes:
            if not _isatty() and asker is ask:
                out(f"Project folder {project} does not exist (not a terminal: pass --yes to "
                    "create it).")
                return False
            answer = asker(f"Project folder {project} does not exist. Create it? [y/N] ")
            if answer not in ("y", "yes"):
                out("Not created; nothing started.")
                return False
        project.mkdir(parents=True)
        out(f"created {project}")
        if cfg.ads.git_init and not (project / ".git").exists():
            cp = subprocess.run(["git", "init", "-q", str(project)], capture_output=True, text=True)
            if cp.returncode == 0:
                out(f"git init {project}")
            else:
                out(f"warning: git init failed: {(cp.stderr or cp.stdout).strip()}")
    (project / "docs").mkdir(exist_ok=True)
    return True


def claude_md_template(name: str | None = None, project: Path | str | None = None) -> str:
    """A new project CLAUDE.md: header naming the project, purpose, empty Lab Notes."""
    if name is None:
        return f"# ads project memory\n\n{LAB_NOTES}\n"
    return (f"# ads project memory: {name}\n\n"
            f"Project: `{project}`\n\n"
            "This file is the shared memory of the ads agents working on this project. It is\n"
            "loaded by every agent of this project's cell (and only this project's). Add lessons\n"
            "with `ads note \"<one line>\"`; do not edit it by hand while a cell runs.\n\n"
            f"{LAB_NOTES}\n")


def ensure_claude_md(path: Path, name: str | None = None,
                     project: Path | str | None = None) -> bool:
    """Make sure `path` has a `## Lab Notes` section; never rewrite existing content.
    A missing file is created from `claude_md_template`. Returns True if the file changed."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(claude_md_template(name, project), encoding="utf-8")
        return True
    if any(line.strip() == LAB_NOTES for line in text.splitlines()):
        return False
    sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{sep}{LAB_NOTES}\n")
    return True


def ensure_gitignore(path: Path, entries: tuple[str, ...] = RUNTIME_GITIGNORE) -> list[str]:
    """Append missing entries to a .gitignore; returns the added ones."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    have = {ln.strip() for ln in text.splitlines()}
    missing = [e for e in entries if e not in have and e.rstrip("/") not in have]
    if missing:
        with open(path, "a", encoding="utf-8") as f:
            if text and not text.endswith("\n"):
                f.write("\n")
            f.write("".join(f"{e}\n" for e in missing))
    return missing


def ensure_runtime(runtime: Path) -> None:
    """`<runtime>/projects/` exists and the runtime .gitignore ignores it (and .venv/)."""
    projects.projects_dir(runtime).mkdir(parents=True, exist_ok=True)
    ensure_gitignore(Path(runtime) / ".gitignore")


def ensure_state(rt: ProjectState, project: Path) -> None:
    """The project's state dir: plan/, plan/drafts/, work/…, CLAUDE.md with Lab Notes."""
    rt.ensure()
    ensure_claude_md(rt.claude_md, rt.name, project)


# --- supervisor pid / session ----------------------------------------------------------

def supervisor_pid(rt: ProjectState) -> int | None:
    """pid of a live supervisor of this runtime, else None.

    Reads the pid file (written after the flock is taken, truncated on release) and checks
    the process exists. Never touches the flock itself: probing it could make a supervisor
    that is just starting fail its LOCK_NB acquire.
    """
    try:
        pid = int(rt.supervisor_pid.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    try:
        os.kill(pid, 0)
    except PermissionError:
        return pid
    except OSError:
        return None
    try:  # guard against pid reuse
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
        if cmd and b"supervisor" not in cmd:
            return None
    except OSError:
        pass
    return pid


def clear_stale_pid(rt: ProjectState) -> None:
    """Truncate a pid file nobody holds (never unlink: the supervisor flocks the inode)."""
    if supervisor_pid(rt) is None and rt.supervisor_pid.exists():
        try:
            with open(rt.supervisor_pid, "r+") as f:
                f.truncate(0)
        except OSError:
            pass


def clear_requests(rt: ProjectState) -> int:
    n = 0
    for p in rt.requests.glob("*.json"):
        p.unlink(missing_ok=True)
        n += 1
    return n


def read_session(rt: ProjectState) -> dict:
    from ads.supervisor import SupervisorError
    from ads.supervisor import read_session as _read
    try:
        return _read(rt)
    except SupervisorError:
        raise StartError(f"no ads session recorded in {rt.session_json}; start one with "
                         "`ads <project>`") from None


def session_tmux(rt: ProjectState, socket: str) -> Tmux:
    return Tmux(socket, conf=runtime_conf(rt.runtime))


def server_sessions(tmux: Tmux) -> list[str] | None:
    """Session names on the server, or None if no server runs on the socket."""
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    cp = subprocess.run([*tmux.base(), "list-sessions", "-F", "#{session_name}"],
                        capture_output=True, text=True, env=env)
    if cp.returncode != 0:
        return None
    return [s for s in cp.stdout.splitlines() if s]


# --- readiness ----------------------------------------------------------------------------

def wait_supervisor(rt: ProjectState, timeout: float = SUPERVISOR_WAIT_S) -> int | None:
    end = time.monotonic() + timeout
    while True:
        pid = supervisor_pid(rt)
        if pid:
            return pid
        if time.monotonic() >= end:
            return None
        time.sleep(POLL_S)


def wait_ready(rt: ProjectState, timeout: float, out: Out = _print) -> dict[str, dict]:
    """Print each agent as it first reaches idle (or a dialog) until all are idle or timeout.
    Returns the final states. Ctrl+C stops waiting (not an error)."""
    t0 = time.monotonic()
    seen: dict[str, str] = {}
    states: dict[str, dict] = {}
    try:
        while True:
            states = agent_state.all_states(rt)
            for a in AGENTS:
                st = states[a]["state"]
                if st in ("idle", "dialog", "down") and seen.get(a) != st:
                    seen[a] = st
                    why = f" ({states[a].get('reason')})" if states[a].get("reason") else ""
                    out(f"  {a:<13}{st}{why}  +{time.monotonic() - t0:.1f}s")
            if all(states[a]["state"] == "idle" for a in AGENTS):
                return states
            if time.monotonic() - t0 >= timeout:
                out(f"readiness: not all agents idle after {timeout:g}s (not fatal):")
                return states
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        out("(stopped waiting for readiness)")
        return agent_state.all_states(rt)


def print_states(states: dict[str, dict], out: Out = _print) -> None:
    for a in AGENTS:
        st = states.get(a, {})
        reason = f" ({st.get('reason')})" if st.get("reason") else ""
        out(f"  {a:<13}{st.get('state', '?')}{reason}")


# --- attach -----------------------------------------------------------------------------------

def nested_warning(prefix: str) -> str:
    return (f"note: you are inside tmux. The ads cell runs on its own tmux server (prefix "
            f"{prefix}); your outer tmux keeps C-b. Detach from ads with {prefix} d. "
            "`switch-client` cannot cross servers, so ads attaches as a nested client. "
            "For Shift+Enter the outer tmux needs `set -s extended-keys on` and "
            "`set -as terminal-features 'xterm*:extkeys'`.")


def attach(rt: ProjectState, socket: str, session: str, prefix: str, out: Out = _print) -> int:
    tmux = session_tmux(rt, socket)
    if not tmux.has_session(session):
        raise StartError(f"no tmux session {session} on socket {socket!r}")
    if not _isatty():
        out(f"not a terminal: not attaching. Attach with `ads attach -p {rt.name}` "
            f"(or `tmux -L {socket} attach -t {session}`).")
        return 0
    argv = [tmux.tmux_bin, "-L", socket, "attach-session", "-t", f"={session}"]
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    if os.environ.get("TMUX"):
        out(nested_warning(prefix))
        return subprocess.run(argv, env=env).returncode
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvpe(argv[0], argv, env)
    return 0  # pragma: no cover


def focus_human(tmux: Tmux, session: str, panes: dict[str, str] | None) -> None:
    try:
        tmux.run("select-window", "-t", f"={session}:0")
        if panes and HUMAN in panes:
            tmux.run("select-pane", "-t", panes[HUMAN])
        else:
            win, idx = LAYOUT[HUMAN]
            tmux.run("select-pane", "-t", f"={session}:{win}.{idx}")
    except TmuxError:
        pass


# --- stop -----------------------------------------------------------------------------------

def stop(rt: ProjectState, out: Out = _print, timeout: float = STOP_WAIT_S) -> int:
    """Graceful stop of one project's cell: supervisor stop request → wait → kill-session
    (+ kill-server if empty). Other projects' cells are on other servers and untouched."""
    from ads.supervisor import write_request
    sess = read_session(rt)
    socket, session = sess["socket"], sess["session"]
    tmux = session_tmux(rt, socket)
    running = tmux.has_session(session)
    pid = supervisor_pid(rt)
    if pid:
        write_request(rt, "stop", **{"from": "cli"})
        end = time.monotonic() + timeout
        while supervisor_pid(rt) and time.monotonic() < end:
            time.sleep(POLL_S)
        if supervisor_pid(rt):
            out(f"supervisor (pid {pid}) did not exit within {timeout:g}s; killing the session")
            for a in AGENTS:
                agent_state.transition(rt, a, "shutdown")
        else:
            out(f"supervisor (pid {pid}) stopped")
    elif running:
        out("supervisor not running; marking agents down(shutdown)")
        for a in AGENTS:
            agent_state.transition(rt, a, "shutdown")
    if running:
        tmux.kill_session(session)
        out(f"killed tmux session {session} (project {rt.name})")
    else:
        out(f"tmux session {session} was not running")
    if not server_sessions(tmux):  # empty, or the server already exited with its last session
        tmux.kill_server()
        _unlink_stale_socket(socket)
    clear_requests(rt)
    out(f"state kept in {rt.dir}; resume with `ads {sess.get('project', '<project>')} --resume`")
    return 0


def stop_all(runtime: Path, out: Out = _print, timeout: float = STOP_WAIT_S) -> int:
    """`ads stop --all`: stop every project whose cell (tmux session or supervisor) is up."""
    targets = [st for st in projects.all_projects(runtime)
               if projects.is_running(st) or supervisor_pid(st)]
    if not targets:
        out("no running ads cells")
        return 0
    for st in targets:
        out(f"== {st.name}")
        stop(st, out, timeout)
    return 0


def _unlink_stale_socket(socket: str) -> None:
    """tmux may leave its socket file behind; remove it once no server answers on it."""
    if "/" in socket:
        return
    base = Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"
    path = base / socket
    if server_sessions(Tmux(socket)) is None and path.is_socket():
        path.unlink(missing_ok=True)


# --- start ------------------------------------------------------------------------------------

def _existing(rt: ProjectState, tmux: Tmux, session: str, project: Path, *, attach_flag: bool,
              restart_flag: bool, out: Out, asker: Callable[[str], str]) -> str:
    """A session for this project exists: returns 'attach' | 'restart' | 'quit'."""
    if attach_flag:
        return "attach"
    if restart_flag:
        return "restart"
    if not _isatty() and asker is ask:
        raise StartError(f"a session for {project} is already running ({session}); "
                         f"pass --attach or --restart (or run `ads attach -p {rt.name}` / "
                         f"`ads stop -p {rt.name}`)")
    answer = asker(f"ads session {session} for {project} is already running. "
                   "[a]ttach / [r]estart / [q]uit? ")
    return {"a": "attach", "attach": "attach", "r": "restart", "restart": "restart"}.get(
        answer, "quit")


def preflight(runtime: Path, cfg: Config, project: Path, out: Out = _print) -> None:
    from ads.cli import FAIL, WARN, doctor_rows
    rows = doctor_rows(runtime, cfg, project)
    bad = [r for r in rows if r[0] in (WARN, FAIL)]
    for status, check, detail in bad:
        out(f"{status}  {check}: {detail}")
    fails = [r for r in rows if r[0] == FAIL]
    if fails:
        raise StartError(f"preflight failed ({len(fails)} FAIL); see `ads doctor {project}`")


def start(runtime: Path, cfg: Config, project_arg: str, *, resume: bool = False,
          no_attach: bool = False, attach_flag: bool = False, restart_flag: bool = False,
          yes: bool = False, config_path: str | None = None, out: Out = _print,
          asker: Callable[[str], str] = ask, run_preflight: bool = True) -> int:
    """`ads <project>`: a bare name is a sibling of the runtime (`<runtime>/../<name>`), any
    other argument a path relative to the current directory."""
    from ads.paths import OverlapError, check_overlap
    from ads.supervisor import cell_env, supervisor_argv, write_session

    runtime = Path(runtime).absolute()
    project = projects.resolve_project_arg(runtime, project_arg)
    out(f"project: {project}")
    try:
        check_overlap(project, runtime)
    except OverlapError as e:
        raise StartError(str(e)) from None

    # 2. project dir, runtime, project state dir
    if not ensure_project(project, cfg, yes=yes, out=out, asker=asker):
        return 0
    ensure_runtime(runtime)
    rt = projects.register(runtime, project)
    ensure_state(rt, project)

    # 3. preflight
    if run_preflight:
        preflight(runtime, cfg, project, out)

    # 4. existing session of THIS project (other projects run on their own servers)
    socket = socket_name(cfg.ads.tmux_socket, rt.name)
    session = session_name(project)
    recorded = projects.read_session(rt)
    if recorded and (recorded["socket"], recorded["session"]) != (socket, session) \
            and projects.is_running(rt):
        socket, session = recorded["socket"], recorded["session"]  # e.g. prefix changed since
    tmux = session_tmux(rt, socket)
    want_attach = cfg.ads.attach and not no_attach
    if tmux.has_session(session):
        action = _existing(rt, tmux, session, project, attach_flag=attach_flag,
                           restart_flag=restart_flag, out=out, asker=asker)
        if action == "quit":
            return 0
        if action == "attach":
            if not want_attach:
                out(f"session {session} is running (not attaching: --no-attach)")
                return 0
            focus_human(tmux, session, _panes(rt))
            return attach(rt, socket, session, cfg.ads.tmux_prefix, out)
        out(f"restarting {session} ...")
        stop(rt, out)
        socket = socket_name(cfg.ads.tmux_socket, rt.name)
        session = session_name(project)
        tmux = session_tmux(rt, socket)
    pid = supervisor_pid(rt)
    if pid:
        raise StartError(f"a supervisor (pid {pid}) still holds {rt.supervisor_pid}; "
                         f"kill it or run `ads stop -p {rt.name}`")

    # 5. agent files, session.json, stale pid / requests
    for agent in AGENTS:
        launcher.render_agent_files(cfg, rt, project, agent)
    if resume:
        missing = [a for a in AGENTS if not launcher.session_file(rt, a).exists()]
        if missing:
            out(f"note: no previous Claude session for {', '.join(missing)}; "
                "they start fresh")
    write_session(rt, socket=socket, session=session, project=project, resume=resume)
    clear_stale_pid(rt)
    clear_requests(rt)

    # 6. layout + supervisor
    sup_cmd = supervisor_argv(
        rt, str(Path(config_path).expanduser().resolve()) if config_path else None)
    t0 = time.monotonic()
    try:
        panes = build_layout(tmux, session, project, rt, supervisor_cmd=sup_cmd,
                             supervisor_env=cell_env(rt, project))
    except TmuxError as e:
        raise StartError(f"cannot build the tmux layout: {e}") from None
    _apply_prefix(tmux, cfg.ads.tmux_prefix)
    out(f"ads session {session} on tmux socket {socket!r} (project {rt.name}: {project}; "
        f"state {rt.dir})")

    # 7. supervisor + readiness
    pid = wait_supervisor(rt)
    if not pid:
        tail = ""
        try:
            tail = tmux.capture(panes["supervisor"], lines=20)
        except (TmuxError, KeyError):
            pass
        raise StartError(f"the supervisor did not start within {SUPERVISOR_WAIT_S:g}s "
                         f"(window 2; {rt.logs / 'supervisor.log'}):\n{tail}\n"
                         f"clean up with `ads stop -p {rt.name}`")
    out(f"supervisor up (pid {pid}) after {time.monotonic() - t0:.1f}s; waiting for agents "
        f"(≤ {cfg.delivery.startup_timeout_s}s, Ctrl+C to skip)")
    states = wait_ready(rt, cfg.delivery.startup_timeout_s, out)
    if not all(states[a]["state"] == "idle" for a in AGENTS):
        print_states(states, out)
        out(f"see `ads status -p {rt.name}`; agents keep starting in the background")
    else:
        out(f"all {len(AGENTS)} agents idle after {time.monotonic() - t0:.1f}s")

    # 8. focus + attach
    focus_human(tmux, session, panes)
    if not want_attach:
        out(f"not attaching; `ads attach -p {rt.name}` to open it, "
            f"`ads stop -p {rt.name}` to stop it")
        return 0
    return attach(rt, socket, session, cfg.ads.tmux_prefix, out)


def _panes(rt: ProjectState) -> dict[str, str] | None:
    try:
        return json.loads(rt.panes_json.read_text())
    except (OSError, ValueError):
        return None


def _apply_prefix(tmux: Tmux, prefix: str) -> None:
    if not prefix or prefix == "C-a":  # ads.tmux.conf default
        return
    try:
        tmux.run("set-option", "-g", "prefix", prefix)
        tmux.run("unbind-key", "C-a", check=False)
        tmux.run("bind-key", prefix, "send-prefix")
    except TmuxError:
        pass


def cmd_attach(rt: ProjectState, cfg: Config, out: Out = _print) -> int:
    sess = read_session(rt)
    tmux = session_tmux(rt, sess["socket"])
    if not tmux.has_session(sess["session"]):
        raise StartError(f"session {sess['session']} is not running; start it with "
                         f"`ads {sess.get('project', '<project>')}`")
    focus_human(tmux, sess["session"], _panes(rt))
    return attach(rt, sess["socket"], sess["session"], cfg.ads.tmux_prefix, out)


# --- list -------------------------------------------------------------------------------------

def list_data(runtime: Path, cfg: Config) -> list[dict]:
    """One row per registered project: name, path, running, socket, session, phase, open tasks."""
    from ads.bus import ledger
    rows = []
    for st in projects.all_projects(runtime):
        sess = projects.read_session(st) or {}
        running = projects.is_running(st)
        rows.append({
            "name": st.name,
            "path": projects.read_info(st).get("path"),
            "running": running,
            "supervisor": supervisor_pid(st),
            # the live cell's socket, else the one the next `ads <project>` will use
            "socket": sess["socket"] if running else socket_name(cfg.ads.tmux_socket, st.name),
            "session": sess.get("session"),
            "phase": ledger.current_phase(st),
            "open_tasks": len(ledger.open_tasks(st)),
            "state_dir": str(st.dir),
        })
    return rows


def print_list(rows: list[dict], out: Out = _print) -> None:
    if not rows:
        out("no projects yet; start one with `ads <project>`")
        return
    table = [("NAME", "PATH", "RUNNING", "SOCKET", "PHASE", "OPEN TASKS")]
    for r in rows:
        table.append((r["name"], r["path"] or "?", "yes" if r["running"] else "no", r["socket"],
                      r["phase"] or "-", str(r["open_tasks"])))
    widths = [max(len(t[i]) for t in table) for i in range(len(table[0]))]
    for t in table:
        out("  ".join(c.ljust(w) for c, w in zip(t, widths)).rstrip())
